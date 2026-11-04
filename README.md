# LovestDNS

LovestDNS 是基于 [PaoPaoDNS](https://github.com/kkkgo/PaoPaoDNS) 的 DNS 服务与 Web 管理面板。DNS 核心、面板、采集器和维护服务可通过 Debian 原生 systemd 服务或 Docker 集成镜像独立运行；DNS 管理 后续可通过只读 API 接入。

当前候选版本：`0.1.0-dev.lovestdns-native.20261104.11`。本仓库为 [LovestDNS](https://github.com/Lovest20018/LovestDNS)，由原 `Lovest20018/PaoPaoDNS` fork 更名；[PaoPaoDNS-WebUI](https://github.com/Lovest20018/PaoPaoDNS-WebUI) 保持独立。原生版运行无需 Docker，安装包以 GitHub 预发布版本提供，尚未宣称稳定版。

## Debian 原生安装

支持 Debian 12/13 amd64、systemd。先在专用主机或独立测试端口验收，再切换客户端 DNS。默认 DNS 为 `0.0.0.0:53/UDP+TCP`，Web 为 `127.0.0.1:8123`；需要局域网访问面板时，在私有配置文件中设置主机内网 IP。

```sh
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/install.py -o install.py
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/.env.native.example -o lovestdns-install.env
chmod 600 lovestdns-install.env
nano lovestdns-install.env
sudo python3 install.py --config ./lovestdns-install.env
sudo lovestdnsctl health
sudo lovestdnsctl password
```

安装入口选择固定版本，下载并校验归档及包内文件，随后调用原生安装器。默认不停止已有 DNS、不修改宿主机解析器、不导入旧目录或关闭 Docker；端口冲突会退出。系统依赖通过 Debian 软件源补齐，Python 依赖已随包离线携带。完整的备份、升级、回退及数据迁移步骤见 [原生部署说明](docs/lovestdns-native-deployment.md)。

## 功能

- 网页编辑环境变量、分流列表、TTL 与高级配置，校验内容并提示生效条件。
- 配置历史、差异、下载与单文件恢复；受控应用 custom_mod，失败时回退旧运行配置。
- 组件状态、两路 Unbound 的 QPS 与命中率、Redis 内存及持久化信息。
- DNS 查询、UDP/TCP 链路对照、诊断报告导入导出与受控 Redis 快照任务。
- 带管理/只读 Token 的 `/api/v1/`，提供实例身份、能力发现、运行指标和 OpenAPI。

上游的递归、分流、加密查询和 Redis 缓存仍由 PaoPaoDNS 核心提供；本项目保留其来源与协议兼容。

## 项目分区

| 目录 / 入口 | 用途 |
| --- | --- |
| `install.py` | 面向部署者的安装包下载、校验和安装入口 |
| `integrated/backend/`、`integrated/runtime/` | Web/API、观测、配置历史与受控 DNS 维护 |
| `integrated/frontend/` | 当前 LovestDNS 界面覆盖层 |
| `packaging/native/` | Debian 安装、升级、回退与原生进程管理 |
| `packaging/integrated/` | Docker 集成版的构建与上游适配 |
| `web/`、`packaging/legacy/` | 冻结的旧 WebUI 依赖与归档发布材料 |
| `scripts/` | 开发构建、API 契约导出、回归和隔离验收 |
| `docs/` | 部署、架构、接口及验证记录 |
| `.build/`、`releases/` | 忽略的本地构建产物；发行包上传 GitHub Releases |

各分区的依赖关系见 [源码结构](docs/project-layout.md)，使用文档入口见 [文档索引](docs/README.md)。

## Docker 兼容部署

在具备 Docker Engine 和 Compose 插件的 Linux 主机中，从当前源码目录执行：

```sh
cp .env.integrated.example .env
# 编辑 .env，至少设置 WEB_UI_TOKEN；可使用 openssl rand -hex 32 生成
nano .env
docker compose -f docker-compose.integrated.yaml build
docker compose -f docker-compose.integrated.yaml up -d
```

默认 DNS 使用宿主机 `53/UDP+TCP`，面板使用 `127.0.0.1:8080`。默认创建独立的数据卷和状态卷。局域网访问面板时，在 `.env` 将 `WEB_BIND_ADDRESS` 设置为主机的内网 IP；数据副本接入使用 `DNS_DATA_DIR` 和 `DNS_STATE_DIR` 指定宿主机绝对路径。

上面是从源码构建 Docker 集成镜像的方式，当前没有公开预构建的 LovestDNS 镜像。PVE CT 内的 Docker 方案及旧数据兼容性核对保留在 [PVE 部署说明](docs/lovestdns-pve-deployment.md)。

## 文档与验证

- [集成版开发、构建与检查](integrated/README.md)
- [集成 API 与 DNS 管理 接入](docs/integration-api.md)
- [API v1 契约与确定性样例](docs/contracts/v1/README.md)
- [DNS 诊断与宿主机快照](docs/dns-diagnostics.md)
- [2026-10-26 Linux amd64 隔离容器验收](docs/verification.md)

已记录的容器验收对应文档中的历史提交，原生测试范围另见 [原生验收记录](docs/verification.md)。尚未完成 PVE 嵌套 Docker、真实生产数据迁移、Debian 12 现场、arm64 和长期负载验收。

## 旧独立 WebUI

旧 sidecar 版已固定为 `standalone-20261024` 源码检查点；`web/` 和原始归档保持冻结。旧部署继续使用 [独立 WebUI 说明](docs/standalone-webui.md)。当前开发和更名针对集成版，构建时通过覆盖层生成 LovestDNS 界面。
