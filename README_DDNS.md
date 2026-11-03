# TP-Link XDR6088 → 阿里云 IPv6 DDNS

按 MAC 从路由器的 `online_host` 表获取设备 IPv6，并同步对应的阿里云 AAAA 记录。一个 MAC 可以对应多个域名，也可以同时关注多台设备。

Docker 部署请参阅同目录的 `README_DOCKER.md`，压缩包已附带 Dockerfile 和 Compose 配置。

需要 Python 3.9+，运行机器能够访问路由器和阿里云 API。运行机器自己不必有公网 IPv6；解析目标来自路由器设备表。

## 安装和配置

解压完整压缩包，保持以下文件在同一目录：

```text
tplink_ddns.py
tplink_online_hosts.py
ddns_config.example.json
requirements.txt
README_DDNS.md
```

安装依赖，将配置模板 `ddns_config.example.json` 复制为本地 `ddns_config.json`：

```bash
python -m pip install -r requirements.txt
cp ddns_config.example.json ddns_config.json
```

编辑 `ddns_config.json`：

- `router.url`：路由器管理地址，仅填写协议、主机和可选端口，不包含 `stok` 路径。
- `router.verify_ssl`：示例为 `false`，适用于路由器自签名 HTTPS 证书；有效证书请设为 `true`。阿里云 API 始终校验证书。
- `interval_seconds`：每次查询路由器后的等待秒数，默认 60。
- `reconcile_seconds`：地址未变化时，每隔多久重新查询阿里云记录，默认 1800。
- `aliyun.ttl`：创建/更新记录时的 TTL，默认 600。可用范围取决于阿里云 DNS 套餐。
- `maclist`：关注的 MAC 和对应的域名记录；MAC 支持横线、冒号、纯十六进制及大小写。

模板中的 `router.example.invalid`、`example.com` 和 MAC 均为占位示例，必须替换为自己的配置。实际配置文件已加入 Git 忽略规则。Windows PowerShell 可用 `Copy-Item ddns_config.example.json ddns_config.json` 复制模板。

例如，只关注 NAS 并同步 `nas.example.com`：

```json
"maclist": [
  {
    "mac": "02:00:00:00:00:01",
    "records": [
      {"domain": "example.com", "rr": "nas"}
    ]
  }
]
```

`domain` 必须是阿里云控制台实际托管的域名区域；`rr` 是该区域下的主机记录：

| domain | rr | 最终域名 |
|---|---|---|
| example.com | nas | nas.example.com |
| example.com | storage.home | storage.home.example.com |
| example.com | @ | example.com |
| home.example.com | nas | nas.home.example.com |

一个 MAC 的 `records` 中可以填多个对象；增加设备就在 `maclist` 中增加对象。同一域名和线路只能绑定一个 MAC。示例配置中的 `example.com` 需要替换为你自己的域名。

## 凭据

默认从以下环境变量读取，不在日志中输出密码、AccessKey、签名或 `stok`。

Linux / macOS / NAS 的 shell：

```bash
export TPLINK_PASSWORD='你的路由器管理员密码'
export ALIBABA_CLOUD_ACCESS_KEY_ID='你的AccessKeyId'
export ALIBABA_CLOUD_ACCESS_KEY_SECRET='你的AccessKeySecret'
```

Windows PowerShell：

```powershell
$env:TPLINK_PASSWORD = '你的路由器管理员密码'
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = '你的AccessKeyId'
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = '你的AccessKeySecret'
```

也可以直接在配置中的 `router` 对象增加 `"password": "管理员密码"`，在 `aliyun` 对象增加 `"access_key_id": "..."` 和 `"access_key_secret": "..."`。环境变量有值时优先使用环境变量；使用本地配置保存凭据时，请限制该文件的读取权限。

阿里云 RAM 身份需要下列 API 权限，并授权给对应域名：

```text
alidns:DescribeSubDomainRecords
alidns:AddDomainRecord
alidns:UpdateDomainRecord
```

如果使用 STS 临时凭据，还可设置 `ALIBABA_CLOUD_SECURITY_TOKEN` 或配置 `aliyun.security_token`。脚本不自动刷新 STS 凭据；固定凭据场景不需要此项。

## 运行

先查询真实路由器和阿里云记录，预览计划，不写 DNS：

```bash
python tplink_ddns.py -c ddns_config.json --once --dry-run
```

确认配置后，执行一次实际同步：

```bash
python tplink_ddns.py -c ddns_config.json --once
```

持续定时运行，启动后立即同步一轮，此后按配置间隔重复，按 Ctrl+C 停止：

```bash
python tplink_ddns.py -c ddns_config.json
```

Linux / NAS 后台运行（继承当前 shell 的环境变量）：

```bash
nohup python -u tplink_ddns.py -c ddns_config.json > ddns.log 2>&1 &
```

以上后台命令不会自动设置开机启动。可交给系统服务管理器或 NAS 任务管理器启动持续运行命令。配置在启动时读取，修改后需重启进程。

`--verbose` 显示缓存跳过信息。`--once` 在任一目标失败或缺少有效 IPv6 时返回退出码 1，全部成功时返回 0。持续运行时，一个目标失败不影响其余目标，后续周期继续重试。

## 同步行为

参考 [ddns-go 的阿里云实现](https://github.com/jeessy2/ddns-go/blob/master/dns/alidns.go) 的查询、比较、创建和更新流程，以及其 [缓存与定时核对思路](https://github.com/jeessy2/ddns-go#系统中使用)：

1. 复用登录后的 Cookie 和 `stok`，查询 `{"hosts_info":{"table":"online_host"},"method":"get"}`。
2. 根据 MAC 选取设备地址，仅使用 `2000::/3` 内、Python 判定为全局地址的 IPv6；`::`、回环、链路本地、ULA、组播、文档示例地址及格式错误值会跳过。
3. 首次运行或 IPv6 变化，立即查询阿里云 AAAA；记录不存在则创建，地址不同则按 RecordId 更新，相同则不写入。IPv6 不同压缩形式按同一地址比较。
4. 地址未变时使用内存缓存，默认每 30 分钟重新与阿里云核对，修复外部修改或删除。仅成功核对或写入后更新缓存，失败在后续周期重试；进程重启后重新核对。
5. 设备离线、没有公网 IPv6，或同一 MAC 返回多个不同公网 IPv6 时，保留已有 DNS 记录。
6. 明确收到会话未授权（HTTP 401/403 或接口 `-40401`）时，重新登录并重试查询一次。登录失败会逐步退避，避免频繁提交错误密码；普通网络错误和 HTTP 500 不会触发重复登录。

记录查询使用 [DescribeSubDomainRecords](https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-describesubdomainrecords)，写入使用 [AddDomainRecord](https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-adddomainrecord) / [UpdateDomainRecord](https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-updatedomainrecord)，签名使用阿里云 RPC HMAC-SHA1。

同一域名、线路有多条 AAAA 时，脚本要求明确填写 `record_id`，避免自动选错。示例：

```json
{"domain": "example.com", "rr": "nas", "line": "default", "record_id": "123456789"}
```

`record_id` 必须属于该区域、主机记录、AAAA 类型和线路；找不到会报错，不会自动新建或改动其它记录。已有记录若被暂停，需要先在阿里云控制台启用。仅管理配置的 AAAA 记录，不删除记录。与 ddns-go 的地址相同比较行为一致：地址相同时不单独调整现有 TTL。

## 固件差异

路由器登录沿用前一个脚本的 XDR `orgAuthPwd` 编码，参考 [TPLinkXDRClient](https://github.com/AlexandrErohin/TP-Link-Archer-C6U/blob/main/tplinkrouterc6u/client/xdr.py)。尚未在你的实机及阿里云账号上联调。

如果管理员密码正确却登录失败，可将 `router.encoded_password` 设为 `true`，并将密码配置为浏览器登录请求中的已编码 `login.password`；登录请求若含 `encrypt_type`，在 `router` 中填写相同类型和值。例如原请求为数字 1，就配置 `"encrypt_type": 1`。带动态 nonce 的新固件不能长期复用抓到的登录密码，需要进一步适配其认证流程。

脚本使用路由器当前提供的设备 IPv6，不会从 MAC 拼接 IPv6，也无法从该单一字段判断它是否为设备的临时隐私地址。DNS 更新后能否访问设备，仍取决于设备服务监听、路由器 IPv6 防火墙及运营商连通性。
