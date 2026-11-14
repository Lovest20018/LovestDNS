# LovestDNS

LovestDNS 是基于 [PaoPaoDNS](https://github.com/kkkgo/PaoPaoDNS) 的 DNS 服务与 Web 管理面板。DNS 核心、面板、采集器和维护服务可通过 Debian 原生 systemd 服务或 Docker 集成镜像独立运行。

最新公开安装包为 [home2.1：组件更新与新控制台](https://github.com/Lovest20018/LovestDNS/releases/tag/v0.1.0-dev.lovestdns-native.20261112.home2.1)（Debian amd64）。Redis 7.2.17、Unbound 1.26.2、DNSCrypt 2.1.20 combined 与新控制台一同交付，保留定制 MosDNS。公开源码、安装包摘要及实际下载验证见[发行记录](docs/native-home21-release-2026-11-12.md)。该安装包不含下述查询记录功能；本项目尚未宣称稳定版或全面提速。

`install.py` 默认下载仍固定 `.20261104.11`。选择新版时使用最新安装脚本并显式传入 `--version 0.1.0-dev.lovestdns-native.20261112.home2.1`；首次配置和实例参数见下方及[部署说明](docs/lovestdns-native-deployment.md)。原验收包与最终公开包摘要不同，以对应 Release 的校验文件为准。

当前主线开发版本为 `0.1.0-dev.lovestdns-native.20261113.query1.1`：包含默认关闭、有容量上限的[真实入口查询记录](docs/query-recording-mvp-2026-11-12.md)、慢入口检查导致的误重置修复，以及查询分区、筛选保留、复制回退和手机布局等六项交互修复。源码状态与验证边界见[本轮主线记录](docs/query11-mainline-2026-11-14.md)。query1 / query1.1 本地候选包已有安装与试用验证，尚未上传公开 Release；最新 UI 修复尚未打入原生安装包。

首页、配置流程和监听采集见[首批实现](docs/home-console-implementation-2026-11-11.md)，实例身份、维护命令及来源见[本轮实现](docs/home2-installation-identity-2026-11-12.md)。原候选安装、升级、回退、测试实例部署和 15 分钟功能观察见[home2.1 验收](docs/home21-validation-results-2026-11-12.md)，公开包的额外验证另列在发行记录中。客户端系统 DNS 未切换。

本仓库为 [LovestDNS](https://github.com/Lovest20018/LovestDNS)，由原 `Lovest20018/PaoPaoDNS` fork 更名；[PaoPaoDNS-WebUI](https://github.com/Lovest20018/PaoPaoDNS-WebUI) 保持独立。原生版运行无需 Docker。源码与历史版本对应关系见[收口说明](docs/source-version-closure-2026-11-06.md)。

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
- 真实入口查询记录默认关闭，仅管理员可查看；来源、域名、类型筛选与分页有界，缓存命中与实际分流路径保持未知。
- 可选的 `/api/v1/` 外部接入，提供管理/只读 Token、实例身份、能力发现、运行指标和 OpenAPI，默认关闭。

网页登录后，在“服务信息 → API 与 Token”开启或关闭外部接入，设置立即生效并保留。关闭不影响网页和 DNS。原生默认实例的 Token 在主机 `/etc/lovestdns/lovestdns.env` 中配置：`SUITE_READ_TOKEN` 用于只读与诊断，`WEB_UI_TOKEN` 用于配置和维护；它们与网页管理员密码独立。详见 [API 认证与角色](docs/integration-api.md#认证与角色)。

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
python3 scripts/build-components.py --output .build/components-bundle
docker compose -f docker-compose.integrated.yaml build
docker compose -f docker-compose.integrated.yaml up -d
```

默认 DNS 使用宿主机 `53/UDP+TCP`，面板使用 `127.0.0.1:8080`。默认创建独立的数据卷和状态卷。局域网访问面板时，在 `.env` 将 `WEB_BIND_ADDRESS` 设置为主机的内网 IP；数据副本接入使用 `DNS_DATA_DIR` 和 `DNS_STATE_DIR` 指定宿主机绝对路径。

上面是从源码构建 Docker 集成镜像的方式，当前没有公开预构建的 LovestDNS 镜像。独立组件候选目前仅支持 amd64，构建与对照验证见 [组件说明](packaging/components/README.md)。PVE CT 内的 Docker 方案及旧数据兼容性核对保留在 [PVE 部署说明](docs/lovestdns-pve-deployment.md)。

## 文档与验证

- [集成版开发、构建与检查](integrated/README.md)
- [集成 API 说明](docs/integration-api.md)
- [API v1 契约与确定性样例](docs/contracts/v1/README.md)
- [DNS 诊断与宿主机快照](docs/dns-diagnostics.md)
- [2026-10-26 Linux amd64 隔离容器验收](docs/verification.md)

验证范围与可复现入口见 [验证说明](docs/verification.md)。现场地址、实例标识、缓存统计及迁移记录不公开发布；单次迁移结果不代表所有旧部署均已验证。同步规则见 [公开材料与本地记录](docs/publication-privacy.md)。

## 旧独立 WebUI

旧 sidecar 版已固定为 `standalone-20261024` 源码检查点；`web/` 和原始归档保持冻结。旧部署继续使用 [独立 WebUI 说明](docs/standalone-webui.md)。当前开发和更名针对集成版，构建时通过覆盖层生成 LovestDNS 界面。
