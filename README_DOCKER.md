# 构建并运行 Docker 镜像

需要已经安装且正在运行的 Docker Engine / Docker Desktop，以及 Docker Compose v2。构建时需能下载 Python 基础镜像和 PyPI 依赖。以下命令在解压后的 `tplink_aliyun_ddns` 目录执行。

本机未安装 Docker，文件已完成静态检查，镜像构建及容器运行仍需在你的 Docker 主机上验证。

群晖 Container Manager 用户请使用 `compose.synology.yaml` 作为项目的完整 Compose 配置。以下通用命令默认使用 `compose.yaml`；在群晖上运行时请改用 `-f compose.synology.yaml`，或将群晖配置复制为原项目的 `docker-compose.yml`。

群晖版不设置 `pids_limit`，以兼容未提供 PIDs cgroup 限制能力的内核，避免启动时出现 `PIDs limit discarded` 警告。通用版仍保留该限制。更新群晖配置后重新创建容器即可生效，无需重新构建镜像。

## 1. 准备配置和凭据

压缩包包含：

```text
Dockerfile
.dockerignore
compose.yaml
.env.example
ddns_config.example.json
requirements.txt
tplink_ddns.py
tplink_online_hosts.py
```

将 `ddns_config.example.json` 复制为 `ddns_config.json`，再填写自己的路由器地址、MAC 和域名。下表仅为虚构示例：

| 示例 IPv4 | 示例 MAC | 示例域名 |
|---|---|---|
| 192.0.2.10 | 02:00:00:00:00:01 | nas.example.com、storage.home.example.com |
| 192.0.2.20 | 02:00:00:00:00:02 | pc.example.com |

将 `router.url` 中的 `https://router.example.invalid` 替换为实际管理地址。文档中的 `192.0.2.0/24` 是示例地址，不用于真实连接。`verify_ssl: false` 适用于路由器自签名 HTTPS 证书，阿里云连接仍校验证书。

将 `.env.example` 复制为 `.env`，填写路由器管理员密码和阿里云 AccessKey：

Linux / NAS / macOS：

```bash
cp .env.example .env
chmod 600 .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
TPLINK_PASSWORD='你的路由器管理员密码'
ALIBABA_CLOUD_ACCESS_KEY_ID='你的AccessKeyId'
ALIBABA_CLOUD_ACCESS_KEY_SECRET='你的AccessKeySecret'
```

Compose 会通过 `environment` 将这些值传入容器。不要把示例占位文字作为真实凭据运行。单引号可保留密码中的 `$`、`#` 和空格；如密码含单引号，可在 `.env` 单引号值中使用 `\'` 转义。

宿主机上已导出的同名环境变量会覆盖 `.env` 中的值。阿里云 RAM 权限和脚本配置详见 `README_DDNS.md`。

## 2. 构建镜像

```bash
docker compose build --pull
```

生成本地镜像 `tplink-aliyun-ddns:local`。也可以单独构建：

```bash
docker build --pull -t tplink-aliyun-ddns:local .
```

Dockerfile 仅复制依赖文件和两个 Python 脚本，`.dockerignore` 也只放行这些构建文件。域名配置和 `.env` 不进入镜像，运行时再挂载/注入。

## 3. 先预览一次

```bash
docker compose run --rm --no-deps ddns --config /config/ddns_config.json --once --dry-run
```

这一步读取真实路由器和阿里云记录，只输出计划，不创建或更新 DNS。检查所配置域名对应的 IPv6 是否正确。没有可用 IPv6 或有任一目标失败时会返回非零退出码，具体原因见日志。

## 4. 启动定时同步

```bash
docker compose up -d
docker compose logs --tail=100 -f ddns
```

启动后立即执行一轮，此后按 `ddns_config.json` 的 `interval_seconds` 重复查询，默认为 60 秒。IPv6 变化立即核对/更新，未变化默认每 30 分钟向阿里云复核。

`restart: unless-stopped` 会在容器退出或 Docker 服务重启后恢复运行，手动停止的容器除外；宿主机开机后也需要 Docker 服务正常启动。Docker Desktop 则需要启用相应的登录时启动设置。

常用操作：

```bash
# 容器状态
docker compose ps

# 停止
docker compose down

# 修改 ddns_config.json 后重新读取配置
docker compose restart ddns

# 修改 .env 后重新创建容器，加载新凭据
docker compose up -d --force-recreate

# 修改 Python 脚本后重新构建并启动
docker compose up -d --build
```

容器默认使用镜像的系统时区，日志一般显示 UTC；程序间隔计时与时区无关。

### 群晖界面日志为空，但 docker logs 有输出

先检查宿主机默认日志驱动和实际容器日志驱动，容器名以实际部署为准：

```bash
sudo docker info --format '默认驱动={{.LoggingDriver}}'
sudo docker inspect --format '容器驱动={{.HostConfig.LogConfig.Type}}' tplink-ddns-ddns-1
```

通用 `compose.yaml` 强制 `json-file` 并配置了该驱动的轮转选项。群晖的原生引擎可使用其专有 `db` 驱动；已有一手问题报告记录了切换到 `json-file` 后 DSM 界面无法正常读取的情况（[参考](https://github.com/gliderlabs/logspout/issues/455)）。这与命令行可读、界面为空的现象相符，但具体主机仍需用上述输出确认。

群晖版 `compose.synology.yaml` 不指定 `logging`，继承宿主机默认驱动；没有将 `db` 写死，也不携带 `json-file` 专用选项。日志保留策略由宿主机管理。它是完整的独立配置，**不要通过两个 `-f` 与通用配置叠加**，否则可能仍继承旧的 `logging`。

已有镜像和项目名 `tplink-ddns` 的部署，可将新文件放到原源码目录，保留 `.env` 和 `ddns_config.json`，然后：

```bash
sudo docker compose -p tplink-ddns -f compose.synology.yaml up -d --no-build --force-recreate
```

此次仅修改容器日志驱动，无需重建镜像。也可直接在原项目的 YAML 配置中移除整个 `logging` 块（含 driver 和 options），然后重新创建容器。项目名以群晖实际名称为准，避免创建第二套项目。若项目原本使用 `docker-compose.yml`，请把群晖版配置同步到该文件，防止之后从群晖界面部署时恢复旧设置。

只重启不会更换日志驱动，必须重新创建；原容器的历史日志不会迁移到新容器。操作后再次 inspect，对比结果是否与宿主机默认驱动一致，再打开新容器的“日志”页。若默认驱动已是 `json-file`，继承默认驱动无法解决这个差异，应继续排查 Container Manager 的日志接口、筛选条件及版本。

程序日志已显式写入 stdout 并逐条刷新，每轮查询完成输出一条摘要。它有助于持续观察，但不能代替日志驱动匹配。命令行查看示例：

```bash
sudo docker logs --tail 100 tplink-ddns-ddns-1
```

如果驱动匹配后仍只有命令行有输出，需要继续排查群晖 Container Manager 的显示问题，记录 DSM 和套件版本。

## 网络和权限

默认使用 Docker bridge 网络，不需要映射端口。容器只需能访问路由器管理地址以及 `https://alidns.aliyuncs.com/`。脚本从路由器读取目标 IPv6，不依赖容器自身获取公网 IPv6，也不需要把 MAC 配置成容器 MAC。

如果容器无法访问你配置的路由器地址，检查宿主机是否连到该路由器、Docker 网段是否与家庭局域网冲突，以及防火墙是否阻止连接。

容器使用 UID/GID `10001:10001`，配置文件以只读方式挂载。宿主机文件必须允许容器用户读取；若你将配置限制为仅宿主用户可读，可在 `compose.yaml` 的 `ddns` 下配置对应的 `user: "宿主UID:宿主GID"`。凭据按推荐方案保存在 Compose 读取的 `.env` 中，无需容器用户读取该文件。

## 可选：将镜像搬到另一台机器

```bash
# 在构建机器导出镜像
docker save -o tplink-aliyun-ddns.tar tplink-aliyun-ddns:local

# 将 tar 和整个配置目录复制到目标机器，在目标机器导入
docker load -i tplink-aliyun-ddns.tar

# 使用已导入镜像，不在目标机器重新构建
docker compose up -d --no-build
```

镜像架构需匹配目标机器。x86 NAS 通常是 `linux/amd64`，ARM64 NAS 通常是 `linux/arm64`；在目标机器本机构建会自动选择本机架构。如果跨架构构建，可用 Docker Buildx，例如为 ARM64 构建并导入本地镜像：

```bash
docker buildx build --platform linux/arm64 -t tplink-aliyun-ddns:local --load .
```

在异构主机上构建需要对应平台的模拟器或原生构建节点。镜像仅保存在本地，以上步骤不会推送到 Docker Hub 或其它镜像仓库。

## 官方文档

- [Docker 镜像构建建议](https://docs.docker.com/build/building/best-practices/)
- [Compose 服务配置](https://docs.docker.com/reference/compose-file/services/)
- [Compose 环境变量优先级](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/)
