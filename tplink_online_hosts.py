#!/usr/bin/env python3
"""登录 TP-Link XDR 路由器，打印在线设备的 MAC、IPv4、IPv6。

依赖：python -m pip install requests
示例：python tplink_online_hosts.py --url https://192.0.2.1 --insecure
登录流程参考：
https://github.com/AlexandrErohin/TP-Link-Archer-C6U/blob/main/tplinkrouterc6u/client/xdr.py
"""

import argparse
import getpass
import json
import sys
from urllib.parse import quote, urlsplit

import requests
import urllib3


def encode_password(password):
    """实现 XDR 常见固件的 orgAuthPwd（按 JavaScript UTF-16 编码）。"""
    key = "RDpbLfCPsJZ7fiv"
    alphabet = (
        "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD"
        "02KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLg"
        "MLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ"
        "1xtXcPcf1aT303wAQhv66qzW"
    )
    raw = password.encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    result = []
    for i in range(max(len(key), len(units))):
        left = ord(key[i]) if i < len(key) else 187
        right = units[i] if i < len(units) else 187
        result.append(alphabet[(left ^ right) % len(alphabet)])
    return "".join(result)


def post_json(session, url, payload, timeout):
    # 不打印异常中的完整 URL，避免把 stok 写进日志。
    try:
        response = session.post(url, json=payload, timeout=timeout, allow_redirects=False)
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            "HTTPS 证书校验失败；若设备使用自签名证书，可添加 --insecure。"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError("请求失败，请检查路由器地址、网络连接和超时设置。") from exc
    if not 200 <= response.status_code < 300:
        raise RuntimeError("路由器返回 HTTP {}。".format(response.status_code))
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("路由器未返回 JSON，请检查管理地址或登录协议。") from exc
    if not isinstance(data, dict):
        raise RuntimeError("路由器返回的 JSON 格式不符合预期。")
    if data.get("error_code") not in (0, "0"):
        raise RuntimeError("路由器接口错误，error_code={!r}。".format(data.get("error_code")))
    return data


def extract_hosts(data):
    """展开 host_info_1、host_info_2 等动态键，保留设备返回的地址。"""
    info = data.get("hosts_info")
    if not isinstance(info, dict) or not isinstance(info.get("online_host"), list):
        raise RuntimeError("返回数据缺少 hosts_info.online_host 列表。")
    result = []
    for entry in info["online_host"]:
        if not isinstance(entry, dict):
            raise RuntimeError("online_host 中存在格式异常的记录。")
        for name, host in entry.items():
            if not name.startswith("host_info_") or not isinstance(host, dict):
                raise RuntimeError("online_host 中存在格式异常的设备记录。")
            result.append({
                "mac": host.get("mac", ""),
                "ipv4": host.get("ip", ""),
                "ipv6": host.get("ipv6", ""),
            })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="路由器管理地址（必填）")
    parser.add_argument("--insecure", action="store_true", help="关闭 HTTPS 证书校验")
    parser.add_argument("--timeout", type=float, default=10, help="请求超时秒数，默认 10")
    parser.add_argument(
        "--encoded-password", action="store_true",
        help="输入浏览器登录请求中的已编码 password，跳过 orgAuthPwd 编码",
    )
    parser.add_argument("--encrypt-type", help="仅当浏览器登录载荷含 encrypt_type 时设置同样的值")
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    parts = urlsplit(base_url)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.path or parts.query or parts.fragment
            or parts.username is not None or parts.password is not None):
        parser.error("--url 应为 http(s)://主机[:端口]，不包含 /stok=.../ds")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    prompt = "登录请求中已编码的 password: " if args.encoded_password else "路由器管理员密码: "
    password = getpass.getpass(prompt)
    if not password:
        parser.error("密码不能为空")
    login = {"password": password if args.encoded_password else encode_password(password)}
    if args.encrypt_type is not None:
        login["encrypt_type"] = args.encrypt_type

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    with requests.Session() as session:
        session.trust_env = False  # 避免把本地管理请求发给环境变量中的代理。
        session.verify = not args.insecure
        session.headers.update({"Referer": base_url + "/", "Origin": base_url})
        try:
            auth = post_json(session, base_url + "/", {"method": "do", "login": login}, args.timeout)
            stok = auth.get("stok")
            if not isinstance(stok, str) or not stok:
                raise RuntimeError("登录响应中没有 stok，可能需要使用该固件的登录编码方式。")
            data = post_json(
                session, base_url + "/stok=" + quote(stok, safe="") + "/ds",
                {"hosts_info": {"table": "online_host"}, "method": "get"}, args.timeout,
            )
            print(json.dumps(extract_hosts(data), ensure_ascii=False, indent=2))
        except RuntimeError as exc:
            print("错误：{}".format(exc), file=sys.stderr)
            if not args.encoded_password:
                print(
                    "若确认密码正确但登录失败，可用 --encoded-password 输入浏览器登录载荷中的 password；"
                    "如载荷含 encrypt_type，再用 --encrypt-type 指定相同值。",
                    file=sys.stderr,
                )
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
