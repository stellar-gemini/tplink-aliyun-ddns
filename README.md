# TP-Link XDR IPv6 DDNS（阿里云）

从 TP-Link XDR6088 路由器的在线设备表中，按 MAC 获取 IPv6，并同步到阿里云 DNS 的 AAAA 记录。支持多个设备、每个设备绑定多个域名。

默认每 60 秒查询路由器，地址变化立即同步，地址未变化每 30 分钟向阿里云复核。设备离线或没有有效公网 IPv6 时保留已有记录。会话失效后重新登录，接口失败后在后续周期重试。

更新流程参考 [ddns-go 的阿里云实现](https://github.com/jeessy2/ddns-go/blob/master/dns/alidns.go)：查询记录，不存在则创建，地址相同则跳过，地址变化则更新。

## Docker 快速开始

需要 Docker Engine / Docker Desktop 和 Docker Compose v2。

**群晖 Container Manager 请使用 `compose.synology.yaml`**，它继承群晖的日志驱动，避免通用配置强制 `json-file` 导致 DSM 界面读不到日志。具体见 [群晖日志配置说明](README_DOCKER.md#群晖界面日志为空但-docker-logs-有输出)。

1. 将 `ddns_config.example.json` 复制为 `ddns_config.json`，填写实际路由器地址和 MAC/域名绑定。
2. 将 `.env.example` 复制为 `.env`，填写路由器密码、阿里云 AccessKeyId 和 AccessKeySecret。
3. 在仓库目录执行：

```bash
docker compose build --pull
docker compose run --rm --no-deps ddns --config /config/ddns_config.json --once --dry-run
docker compose up -d
docker compose logs --tail=100 -f ddns
```

`--dry-run` 只读取路由器和阿里云记录并输出同步计划。持续运行命令会实际创建或更新配置的 AAAA 记录。

- [Docker 构建与部署说明](README_DOCKER.md)
- [配置、凭据、RAM 权限及同步行为](README_DDNS.md)
- [通用配置模板](ddns_config.example.json)

## 配置记录

```json
{
  "mac": "02:00:00:00:00:01",
  "records": [
    {"domain": "example.com", "rr": "nas"},
    {"domain": "example.com", "rr": "syn"}
  ]
}
```

上例会更新 `nas.example.com` 和 `syn.example.com` 的 AAAA。`rr` 填主机记录，主域名本身填 `@`。同一域名、线路有多条 AAAA 时需要显式填写 `record_id`。

仓库仅包含配置模板。实际配置 `ddns_config.json` 与凭据文件 `.env` 均加入了 Git 忽略规则，构建上下文也排除了凭据及运行配置。

文档及测试使用虚构 MAC、`example.com` / `.invalid` 域名以及文档专用 IP 地址；请在本地填写自己的配置，勿提交实际凭据和网络信息。测试使用 `2001:db8::/32` 文档地址，Runner 测试仅为两个固定样本注入地址分类器；生产代码仍拒绝文档地址，独立测试覆盖该行为。

## Python 直接运行

需要 Python 3.9+。设置好配置和环境变量后：

```bash
python -m pip install -r requirements.txt
python tplink_ddns.py -c ddns_config.json --once --dry-run
python tplink_ddns.py -c ddns_config.json
```

## 验证

```bash
python -B -m unittest discover -s tests -v
```

当前包含 24 项模拟测试，覆盖阿里云官方签名样例、记录创建/更新/跳过、缓存与周期复核、会话过期、失败重试、多记录选择、预览模式、日志输出及文档地址过滤。测试不访问真实路由器或阿里云账号。Docker 镜像尚未在当前开发环境构建验证。
