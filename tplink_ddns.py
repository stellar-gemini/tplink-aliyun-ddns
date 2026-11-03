#!/usr/bin/env python3
"""从 TP-Link XDR 在线设备表获取 IPv6，并同步阿里云 AAAA 记录。Python 3.9+。"""

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
import urllib3

from tplink_online_hosts import encode_password, extract_hosts

LOG = logging.getLogger("tplink-ddns")
GLOBAL_V6 = ipaddress.IPv6Network("2000::/3")


class DDNSError(Exception):
    """可以安全打印的错误，不含完整请求 URL 或凭据。"""


class SessionExpired(DDNSError):
    pass


def normalize_mac(value):
    if not isinstance(value, str):
        raise ValueError("MAC 必须为字符串")
    value = value.strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{12}", value):
        raise ValueError("MAC 格式错误")
    return value.replace(":", "").replace("-", "")


def global_ipv6(value):
    if not isinstance(value, str) or "/" in value or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if (isinstance(address, ipaddress.IPv6Address) and address.is_global
            and address in GLOBAL_V6 and not address.is_multicast):
        return str(address)
    return None


def positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(label + " 必须为正数")
    return value


def dns_name(value):
    if not isinstance(value, str):
        raise ValueError("域名必须为字符串")
    name = value.rstrip(".").encode("idna").decode("ascii").lower()
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)):
        raise ValueError("域名格式错误")
    return name


@dataclass(frozen=True)
class Binding:
    mac: str
    domain: str
    rr: str
    line: str = "default"
    record_id: str = ""

    @property
    def fqdn(self):
        return self.domain if self.rr == "@" else self.rr + "." + self.domain


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("配置必须为 JSON 对象")
    router = cfg.get("router")
    aliyun = cfg.get("aliyun")
    if not isinstance(router, dict) or not isinstance(aliyun, dict):
        raise ValueError("配置缺少 router 或 aliyun")
    url = router.get("url", "").rstrip("/")
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path
            or parts.query or parts.fragment or parts.username is not None):
        raise ValueError("router.url 应为 http(s)://主机[:端口]")
    router["url"] = url
    for key in ("verify_ssl", "encoded_password"):
        if key in router and not isinstance(router[key], bool):
            raise ValueError("router." + key + " 必须为布尔值")
    cfg["interval_seconds"] = positive_number(cfg.get("interval_seconds", 60), "interval_seconds")
    cfg["reconcile_seconds"] = positive_number(cfg.get("reconcile_seconds", 1800), "reconcile_seconds")
    cfg["timeout_seconds"] = positive_number(cfg.get("timeout_seconds", 10), "timeout_seconds")
    ttl = aliyun.get("ttl", 600)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("aliyun.ttl 必须为正整数")
    aliyun["ttl"] = ttl
    devices = cfg.get("maclist")
    if not isinstance(devices, list) or not devices:
        raise ValueError("maclist 必须为非空列表")
    bindings, used = [], set()
    for device in devices:
        mac = normalize_mac(device["mac"])
        records = device.get("records")
        if not isinstance(records, list) or not records:
            raise ValueError("每个 MAC 必须配置非空 records")
        for record in records:
            domain = dns_name(record["domain"])
            rr = record["rr"].strip().rstrip(".").lower()
            if rr != "@":
                fqdn = dns_name(rr + "." + domain)
                rr = fqdn[:-(len(domain) + 1)]
            line = record.get("line", "default")
            record_id = record.get("record_id", "")
            if not isinstance(line, str) or not line or not isinstance(record_id, str):
                raise ValueError("line 必须为非空字符串，record_id 必须为字符串")
            binding = Binding(mac, domain, rr, line, record_id)
            key = (binding.fqdn, line)
            if key in used:
                raise ValueError("同一域名和线路只能绑定一个 MAC：" + binding.fqdn)
            used.add(key)
            bindings.append(binding)
    cfg["bindings"] = bindings
    return cfg


def secret(config, field, default_env):
    env_name = config.get(field + "_env", default_env)
    value = os.environ.get(env_name) or config.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError("请设置环境变量 {} 或配置字段 {}".format(env_name, field))
    return value


class RouterClient:
    def __init__(self, cfg, timeout):
        self.url = cfg["url"]
        self.timeout = timeout
        password = secret(cfg, "password", "TPLINK_PASSWORD")
        self.login_body = {"password": password if cfg.get("encoded_password", False) else encode_password(password)}
        if "encrypt_type" in cfg:
            self.login_body["encrypt_type"] = cfg["encrypt_type"]
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = cfg.get("verify_ssl", True)
        self.session.headers.update({"Referer": self.url + "/", "Origin": self.url})
        if not self.session.verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.stok = None
        self.login_failures = 0
        self.next_login = 0

    def post(self, path, payload):
        try:
            response = self.session.post(self.url + path, json=payload,
                                         timeout=self.timeout, allow_redirects=False)
        except requests.exceptions.SSLError:
            raise DDNSError("路由器 HTTPS 证书校验失败；自签名证书可配置 verify_ssl=false") from None
        except requests.RequestException:
            raise DDNSError("路由器请求失败或超时") from None
        if response.status_code in (401, 403):
            raise SessionExpired("路由器会话未授权")
        if not 200 <= response.status_code < 300:
            raise DDNSError("路由器 HTTP {}".format(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise DDNSError("路由器未返回 JSON") from None
        if not isinstance(data, dict):
            raise DDNSError("路由器响应格式错误")
        code = data.get("error_code")
        if str(code) == "-40401":
            raise SessionExpired("路由器登录会话已失效")
        if code not in (0, "0"):
            raise DDNSError("路由器接口失败（请核对固件登录协议及密码）")
        return data

    def login(self):
        if time.monotonic() < self.next_login:
            raise DDNSError("登录失败后的退避等待中，请检查密码或登录协议")
        self.stok = None
        self.session.cookies.clear()
        try:
            data = self.post("/", {"method": "do", "login": self.login_body})
            stok = data.get("stok")
            if not isinstance(stok, str) or not stok:
                raise DDNSError("登录响应缺少 stok")
        except DDNSError:
            self.login_failures += 1
            self.next_login = time.monotonic() + min(60 * 2 ** min(self.login_failures - 1, 4), 900)
            raise
        self.stok = stok
        self.login_failures = 0
        self.next_login = 0
        LOG.info("路由器登录成功")

    def get_hosts(self):
        if not self.stok:
            self.login()
        payload = {"hosts_info": {"table": "online_host"}, "method": "get"}
        for attempt in range(2):
            try:
                data = self.post("/stok=" + quote(self.stok, safe="") + "/ds", payload)
                return extract_hosts(data)
            except SessionExpired:
                self.stok = None
                if attempt:
                    raise DDNSError("重新登录后查询仍未授权") from None
                LOG.info("会话失效，重新登录后重试一次")
                self.login()
            except RuntimeError:
                raise DDNSError("在线设备表格式错误") from None

    def close(self):
        self.session.close()


def aliyun_signature(params, access_key_secret):
    def percent(value):
        return quote(str(value), safe="~")
    canonical = "&".join(percent(key) + "=" + percent(params[key]) for key in sorted(params))
    string_to_sign = "GET&%2F&" + percent(canonical)
    digest = hmac.new((access_key_secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class AliDNS:
    ENDPOINT = "https://alidns.aliyuncs.com/"

    def __init__(self, cfg, timeout):
        self.key_id = secret(cfg, "access_key_id", "ALIBABA_CLOUD_ACCESS_KEY_ID")
        self.key_secret = secret(cfg, "access_key_secret", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        self.security_token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN") or cfg.get("security_token", "")
        self.timeout = timeout
        self.ttl = cfg["ttl"]
        self.session = requests.Session()
        self.session.trust_env = False

    def call(self, action, **kwargs):
        params = {
            "Action": action, "Version": "2015-01-09", "Format": "JSON",
            "AccessKeyId": self.key_id, "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **{key: str(value) for key, value in kwargs.items()},
        }
        if self.security_token:
            params["SecurityToken"] = self.security_token
        params["Signature"] = aliyun_signature(params, self.key_secret)
        try:
            response = self.session.get(self.ENDPOINT, params=params, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise DDNSError("阿里云 {} 请求失败或超时".format(action)) from None
        try:
            data = response.json()
        except ValueError:
            raise DDNSError("阿里云 {} 响应不是 JSON（HTTP {}）".format(action, response.status_code)) from None
        if not isinstance(data, dict):
            raise DDNSError("阿里云响应格式错误")
        if data.get("Code"):
            code = re.sub(r"[^a-zA-Z0-9_.-]", "", str(data["Code"]))[:100]
            raise DDNSError("阿里云 {} 失败：{}".format(action, code))
        if not 200 <= response.status_code < 300:
            raise DDNSError("阿里云 HTTP {}".format(response.status_code))
        return data

    def records(self, binding):
        records, page = [], 1
        while True:
            data = self.call("DescribeSubDomainRecords", DomainName=binding.domain,
                             SubDomain=binding.fqdn, Type="AAAA", Line=binding.line,
                             PageNumber=page, PageSize=100)
            block = data.get("DomainRecords")
            rows = block.get("Record") if isinstance(block, dict) else None
            total = data.get("TotalCount")
            if (not isinstance(rows, list) or not isinstance(total, int) or total < 0
                    or any(not isinstance(row, dict) for row in rows)):
                raise DDNSError("阿里云记录列表格式错误")
            records.extend(rows)
            if len(records) >= total:
                break
            if not rows or page >= 1000:
                raise DDNSError("阿里云记录分页未完成")
            page += 1
        # 精确限制到配置的区域、主机记录和线路。
        return [r for r in records if r.get("Type") == "AAAA"
                and r.get("DomainName", "").lower().rstrip(".") == binding.domain
                and r.get("RR", "").lower() == binding.rr
                and r.get("Line", "default") == binding.line]

    def sync(self, binding, ipv6, dry_run=False):
        records = self.records(binding)
        if binding.record_id:
            selected = [r for r in records if str(r.get("RecordId")) == binding.record_id]
            if len(selected) != 1:
                raise DDNSError("指定的 record_id 未在该域名、AAAA 类型和线路中找到")
            record = selected[0]
        elif len(records) > 1:
            raise DDNSError("该域名和线路存在多条 AAAA，请配置 record_id")
        else:
            record = records[0] if records else None
        if record and record.get("Status") != "ENABLE":
            # API 文档及实际响应使用 Enable，兼容大小写。
            if str(record.get("Status", "")).lower() != "enable":
                raise DDNSError("已有 AAAA 记录未启用，请先在阿里云控制台启用")
        if record:
            try:
                unchanged = ipaddress.ip_address(record.get("Value", "")) == ipaddress.ip_address(ipv6)
            except ValueError:
                unchanged = False
            if unchanged:
                LOG.info("%s AAAA 未变化：%s", binding.fqdn, ipv6)
                return "unchanged"
        action = "UpdateDomainRecord" if record else "AddDomainRecord"
        params = dict(RR=binding.rr, Type="AAAA", Value=ipv6, TTL=self.ttl, Line=binding.line)
        if record:
            if not record.get("RecordId"):
                raise DDNSError("阿里云记录缺少 RecordId")
            params["RecordId"] = record["RecordId"]
        else:
            params["DomainName"] = binding.domain
        if dry_run:
            LOG.info("[预览] %s %s AAAA -> %s", action, binding.fqdn, ipv6)
            return "dry-run"
        result = self.call(action, **params)
        if not result.get("RecordId"):
            raise DDNSError("阿里云写入响应缺少 RecordId，下一轮将重新查询")
        LOG.info("%s %s AAAA -> %s 成功", action, binding.fqdn, ipv6)
        return "updated" if record else "created"

    def close(self):
        self.session.close()


class Runner:
    def __init__(self, router, dns, bindings, reconcile_seconds, dry_run=False):
        self.router, self.dns, self.bindings = router, dns, bindings
        self.reconcile_seconds, self.dry_run = reconcile_seconds, dry_run
        self.cache = {}  # 只缓存成功核对/写入的结果；重启后立即与服务商核对。

    def cycle(self):
        try:
            hosts = self.router.get_hosts()
        except DDNSError as exc:
            LOG.error("本轮查询路由器失败：%s", exc)
            return False
        by_mac = {}
        for host in hosts:
            try:
                mac = normalize_mac(host.get("mac"))
            except ValueError:
                continue
            address = global_ipv6(host.get("ipv6"))
            by_mac.setdefault(mac, set())
            if address:
                by_mac[mac].add(address)
        ok = True
        counts = dict(created=0, updated=0, unchanged=0, cached=0, skipped=0, failed=0)
        previewed = 0
        for binding in self.bindings:
            addresses = by_mac.get(binding.mac, set())
            if len(addresses) != 1:
                LOG.warning("%s：设备离线、无唯一公网 IPv6，跳过并保留原记录", binding.fqdn)
                counts["skipped"] += 1
                ok = False
                continue
            ipv6 = next(iter(addresses))
            now = time.monotonic()
            cached = self.cache.get(binding)
            if (cached and cached[0] == ipv6 and now - cached[1] < self.reconcile_seconds
                    and not self.dry_run):
                LOG.debug("%s 地址未变，使用缓存", binding.fqdn)
                counts["cached"] += 1
                continue
            try:
                result = self.dns.sync(binding, ipv6, self.dry_run)
                if result == "dry-run":
                    previewed += 1
                else:
                    counts[result] += 1
                    self.cache[binding] = (ipv6, time.monotonic())
            except DDNSError as exc:
                LOG.error("%s 同步失败：%s", binding.fqdn, exc)
                counts["failed"] += 1
                ok = False
        LOG.info(
            "本轮完成：在线设备 %d，新增 %d，更新 %d，无变化 %d，缓存跳过 %d，设备跳过 %d，失败 %d，预览 %d",
            len(by_mac), counts["created"], counts["updated"], counts["unchanged"],
            counts["cached"], counts["skipped"], counts["failed"], previewed,
        )
        return ok


def configure_logging(verbose=False):
    # 明确写入 stdout，兼容容器日志界面；StreamHandler 每条日志都会 flush。
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout, force=True)
    # 请求调试日志会包含 URL 中的 stok 或阿里云签名。
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=str(Path(__file__).with_name("ddns_config.json")))
    parser.add_argument("--once", action="store_true", help="执行一轮后退出，失败返回非零状态码")
    parser.add_argument("--dry-run", action="store_true", help="查询路由器和阿里云，打印计划但不写 DNS")
    parser.add_argument("--verbose", action="store_true", help="显示缓存跳过日志")
    args = parser.parse_args()
    configure_logging(args.verbose)
    router = dns = None
    try:
        cfg = load_config(args.config)
        router = RouterClient(cfg["router"], cfg["timeout_seconds"])
        dns = AliDNS(cfg["aliyun"], cfg["timeout_seconds"])
        runner = Runner(router, dns, cfg["bindings"], cfg["reconcile_seconds"], args.dry_run)
        stop = threading.Event()
        def stop_handler(signum, frame):
            stop.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop_handler)
        LOG.info("启动：%d 条 MAC/AAAA 绑定，每 %s 秒查询，每 %s 秒向服务商复核",
                 len(cfg["bindings"]), cfg["interval_seconds"], cfg["reconcile_seconds"])
        while not stop.is_set():
            ok = runner.cycle()
            if args.once:
                return 0 if ok else 1
            stop.wait(cfg["interval_seconds"])
        LOG.info("已停止")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, DDNSError) as exc:
        # 配置内容可能含密码，不直接打印 JSON 解析异常或任意外部异常文本。
        if isinstance(exc, DDNSError):
            LOG.error("%s", exc)
        else:
            LOG.error("配置或启动失败（%s），请检查配置结构、文件路径、字段及环境变量", type(exc).__name__)
        return 1
    finally:
        if router:
            router.close()
        if dns:
            dns.close()


if __name__ == "__main__":
    sys.exit(main())
