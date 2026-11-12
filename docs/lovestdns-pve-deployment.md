# LovestDNS：PVE CT 部署与旧数据迁移

本文保留 `0.1.0-dev.lovestdns.20261104.1` 的 **Docker 方案**：PVE → Debian CT → Docker → LovestDNS 集成容器。后续新增的不依赖 Docker 的 systemd 安装包见 [Debian 原生部署](lovestdns-native-deployment.md)。

2026-10-26 已完成历史运行提交的 Linux amd64 隔离容器验收。本次更名不等于已经完成目标 CT 的嵌套 Docker、真实旧数据加载或长期负载验收。先用副本验证，再切换 DNS 客户端。

## 1. 新建 CT

以下为一般场景的起始配置，后续按实际使用量调整：

| 项目 | 建议 |
|---|---|
| 模板 | Debian 13 amd64；Debian 12 也可 |
| CPU | 2 vCPU |
| 内存 | 4 GiB；留出 Redis 保存快照和镜像构建的余量 |
| 磁盘 | 至少 20 GiB；镜像、数据副本、备份与构建缓存均会占空间 |
| 网络 | 独立固定内网 IP，桥接到客户端可达的网络 |
| 类型 | 非特权 CT，启用 Nesting 和 keyctl |
| 开机启动 | 启用；待外部网络可用后启动 |

PVE 的 `keyctl` 选项明确用于非特权 CT 内运行 Docker，`nesting` 允许嵌套运行。具体定义见 [PVE 官方选项文档](https://github.com/proxmox/pve-docs/blob/master/generated/pct.conf.5-opts.adoc)。这里没有要求将 CT 改为特权模式或关闭 AppArmor；若 Docker 因宿主机内核、存储或权限问题失败，应核对具体错误，必要时改用 Debian VM。

CT 初始化期间使用可工作的外部 DNS，避免依赖尚未启动的 LovestDNS。按 [Docker 官方 Debian 安装文档](https://docs.docker.com/engine/install/debian/) 安装 Docker Engine、Buildx 与 Compose 插件。当前官方支持 Debian 13/12。

先在 CT 内检查：

```sh
docker version
docker compose version
docker info
docker run --rm hello-world
ss -lntup
```

确认 Docker 可实际创建容器，53/TCP、53/UDP 和计划使用的 Web 端口无冲突。后续还需在客户端验证端口可达；只在 CT 内启动成功不代表局域网已可使用。

## 2. 准备源码和环境

当前没有已发布的 LovestDNS 预构建镜像。将包含本次更名的源码复制到 CT 的 `/opt/lovestdns/source`，记录该源码提交或提供方源码摘要；直接拉旧远端分支未必含本次改动。

在 CT 内执行：

```sh
install -d -m 700 /opt/lovestdns/data /opt/lovestdns/state
cd /opt/lovestdns/source
cp .env.integrated.example .env
chmod 600 .env
openssl rand -hex 32
nano .env
```

把生成的随机值填入 `WEB_UI_TOKEN`，并按实际环境修改以下项。示例 IP `192.0.2.60` 必须换成新 CT 的真实内网 IP：

```dotenv
PAOPAODNS_SUITE_IMAGE=lovestdns:0.1.0-dev
WEB_UI_TOKEN=填入生成的随机值
SUITE_READ_TOKEN=
SUITE_INSTANCE_NAME=LovestDNS
DNS_BIND_ADDRESS=0.0.0.0
DNS_HOST_PORT=53
WEB_BIND_ADDRESS=192.0.2.60
WEB_HOST_PORT=8080
DNS_DATA_DIR=/opt/lovestdns/data
DNS_STATE_DIR=/opt/lovestdns/state
```

不使用只读 API 时，`SUITE_READ_TOKEN` 可留空；需要接入时设置一个与管理 Token 不同的随机值即可。面板默认绑定回环地址，必须明确设置 `WEB_BIND_ADDRESS` 才能从其他电脑访问。

`PAOPAODNS_SUITE_IMAGE`、Compose 服务键 `paopaodns-suite`、容器内 `/var/lib/paopaodns-suite` 和原有 named volume 默认名保留兼容。项目对外名称为 LovestDNS，DNS 核心来源仍标明 PaoPaoDNS。已使用集成版 named volume 的部署不要随意更换 Compose 项目名，否则可能创建空的新卷。

两个持久目录用途不同：

- `/opt/lovestdns/data → /data`：原 DNS 配置、规则、模板、Redis RDB 等。
- `/opt/lovestdns/state → /var/lib/paopaodns-suite`：新面板配置历史、实例身份、任务记录和日志。

旧上游实例迁入时使用全新的状态目录。不要把它放在 `/data` 内。首次接入保留的“初始输入副本”仅覆盖已知配置文件，不代替完整数据与缓存备份。

## 3. 迁移兼容性

迁移前核对旧核心的镜像摘要、Redis/Unbound 版本、数据目录与环境变量。使用完整备份的副本验证 RDB 读取与实际 DNS 响应；不要让两个核心共享可写数据。内存缓存不会随 RDB 完整迁移。

## 4. 复制旧数据并构建

在约定的维护窗口执行实际备份。以下是后续迁移操作流程，本次只读核对没有执行这些步骤：

1. 在旧机保存容器部署配置、实际环境变量和镜像 digest，备份文件设为仅管理员可读；环境变量可能含代理口令。除 Compose 模板中列出的项，还要核对其他旧变量及 `/data/custom_env.ini` 中的覆盖值。
2. 在旧 Redis 执行 `BGSAVE` 并等待完成，确认 `rdb_bgsave_in_progress=0`、`rdb_last_bgsave_status=ok`、`LASTSAVE` 已推进。保存失败时先处理磁盘或内存问题。
3. 暂停旧核心及旧面板的配置写入，完整归档 `<OLD_DATA_DIR>` 到独立备份位置。保留文件、目录、权限及符号链接，并额外保存 `/data` 外的自定义引用文件。不要只复制几个常见配置或只复制 RDB。
4. 将归档传输到新 CT，在尚未启动新容器时解压到空的 `/opt/lovestdns/data`。在两端比对归档 SHA-256，并对解压后的 RDB、关键配置再比对校验和。旧原件和独立备份保留供回退。
5. 如旧实例要继续提供服务，可在复制完成后恢复；新实例始终只写自己的副本。正式切换前若还要保留最新缓存，再做一份最终快照。两个核心不得共享同一份可写 `/data`。
6. 将 Compose 已列出的旧业务环境变量同步到新 `.env`；其他变量需要同时补到 Compose 的 `environment` 中，或保留在 `/data/custom_env.ini` 中。仅在 `.env` 新增名字不会自动传入容器。检查新 CT 可访问的上游 DNS、代理和 IPv6 条件，不要把新模板默认值当作旧实例的原配置。

备份与传输命令应在拿到新 CT 的连接信息后按实际路径生成；此时不预填未知目标，也不改动 source-host。测试副本启动后会产生写入，正式切换和回退都应使用保留的备份，不能把测试后数据误当作未改动原件。

数据准备好后，在 CT 的源码目录执行：

```sh
docker compose -f docker-compose.integrated.yaml config --quiet
python3 scripts/build-components.py --output .build/components-bundle
docker compose -f docker-compose.integrated.yaml build
docker compose -f docker-compose.integrated.yaml up -d
docker compose -f docker-compose.integrated.yaml ps
```

构建仍依赖外部镜像仓库与系统包源。固定的上游 digest 不代表所有 Alpine 系统依赖已经完全可复现。RDB 加载时间取决于实际容量，首次加载时应观察 Redis 日志和容器内存；健康状态尚未就绪时先排查加载进度及错误，不要反复重建或删除数据。

## 5. 切换前验证

先保持现有客户端 DNS 指向旧实例，从一台测试电脑直连新 CT：

```sh
# 将 192.0.2.60 换成新 CT 的真实 IP
dig @192.0.2.60 example.com A
dig +tcp @192.0.2.60 example.com A
dig @192.0.2.60 example.com AAAA
```

还需查询旧实例实际使用的强制分流、TTL、Hosts 或私有域名，比较预期响应。公网例子能解析不代表自定义规则已经生效。

在 CT 中检查 Redis：

```sh
docker compose -f docker-compose.integrated.yaml exec -T paopaodns-suite redis-cli -s /tmp/redis.sock PING
docker compose -f docker-compose.integrated.yaml exec -T paopaodns-suite redis-cli -s /tmp/redis.sock INFO persistence
docker compose -f docker-compose.integrated.yaml exec -T paopaodns-suite redis-cli -s /tmp/redis.sock INFO keyspace
```

确认 RDB 成功加载、`loading=0`、DB 0/1 有合理键数，检查 Redis 日志无 RDB 格式/权限错误或 OOM。由于 TTL 正常流逝，键数不要求与旧检查时完全相等；抽样缓存查询及 Redis 命中计数只能证明被测样本。

打开 `http://新CT内网IP:8080`，输入管理 Token，确认 LovestDNS 名称、版本、各组件状态、采样新鲜度，以及 DNS 查询和配置读取。然后依次验证容器重启、CT 重启后的自动启动、数据保留与 UDP/TCP 解析。

全部通过后，再将 ROS/DHCP 或客户端 DNS 指向新 CT；保留旧服务与原始备份，观察实际客户端。需要回退时先把客户端 DNS 恢复到旧 IP，再恢复旧镜像与旧备份。不要以 `docker compose down -v` 作为升级或回退步骤，它会删除 named volume。

外部客户端可通过 `/api/v1/` 接入同一实例。
