# LovestDNS

LovestDNS 是基于 [PaoPaoDNS](https://github.com/kkkgo/PaoPaoDNS) 的 DNS 服务与 Web 管理面板。DNS 核心、面板、采集器和维护服务可通过 Debian 原生 systemd 服务或 Docker 集成镜像独立运行。

**当前主线版本：query1.3**（`0.1.0-dev.lovestdns-native.20261114.query1.3`，Debian amd64 预发布）。新增网页软件更新，保留真实入口查询记录、采集时钟修复和全部六项 UI / 交互修复。home2.1 为历史版本。

[下载主线 query1.3](https://github.com/Lovest20018/LovestDNS/releases/tag/v0.1.0-dev.lovestdns-native.20261114.query1.3) · [发行与验收记录](docs/native-query13-release-2026-11-14.md) · [安装、升级与回退](docs/lovestdns-native-deployment.md)

主线支持通过网页升级官方原生包，同时包含查询分区与直达、刷新状态区分、复制反馈与回退、筛选保留、手机记录卡片和明暗主题链接改进。查询记录默认关闭，仅管理员可查看；启用后按时间及容量保留，缓存命中和逐请求分流路径保持未知。Redis 7.2.17、Unbound 1.26.2、DNSCrypt 2.1.20 combined 与定制 MosDNS 保持已验收的组件字节。

发行包的应用和前端固定到提交 `03b8f70`，已完成同包安装／升级／回退、500 次计数查询、完整 Linux CI 和匿名下载验证；真实 systemd 更新助手在合成下载边界下完成成功升级及失败恢复。仍按预发布提供，不宣称长期稳定性或全面提速；[查询记录范围](docs/query-recording-mvp-2026-11-12.md)列出观测限制。

**安装入口默认选择 query1.3。** 旧版本先用此 Release 随附的安装入口升级一次，后续可在 **设置 → 服务信息 → 软件更新** 检查、确认并升级。升级前校验和备份，DNS 与网页会短暂重启；失败会尝试恢复原版，关闭网页不取消任务。不同版本的摘要不能混用，以对应 Release 的校验文件为准。

本仓库为 [LovestDNS](https://github.com/Lovest20018/LovestDNS)，由原 `Lovest20018/PaoPaoDNS` fork 更名；[PaoPaoDNS-WebUI](https://github.com/Lovest20018/PaoPaoDNS-WebUI) 保持独立。原生版运行无需 Docker。源码与历史版本对应关系见[收口说明](docs/source-version-closure-2026-11-06.md)。

## Debian 原生安装

支持 Debian 12/13 amd64、systemd。先在专用主机或独立测试端口验收，再切换客户端 DNS。默认 DNS 为 `0.0.0.0:53/UDP+TCP`，Web 为 `127.0.0.1:8123`；需要局域网访问面板时，在私有配置文件中设置主机内网 IP。

```sh
curl -fL https://github.com/Lovest20018/LovestDNS/releases/download/v0.1.0-dev.lovestdns-native.20261114.query1.3/install.py -o install.py
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/.env.native.example -o lovestdns-install.env
chmod 600 lovestdns-install.env
nano lovestdns-install.env
sudo python3 install.py --version 0.1.0-dev.lovestdns-native.20261114.query1.3 --config ./lovestdns-install.env
sudo lovestdnsctl health
sudo lovestdnsctl password
```

已有命名实例升级时保留原配置，使用原实例名；默认实例省略 `--instance`：

```sh
sudo python3 install.py --version 0.1.0-dev.lovestdns-native.20261114.query1.3 --instance <实例名>
```

安装入口选择固定版本，下载并校验归档及包内文件，随后调用原生安装器。默认不停止已有 DNS、不修改宿主机解析器、不导入旧目录或关闭 Docker；端口冲突会退出。系统依赖通过 Debian 软件源补齐，Python 依赖已随包离线携带。完整的备份、升级、回退及数据迁移步骤见 [原生部署说明](docs/lovestdns-native-deployment.md)。

## 功能

- 网页编辑环境变量、分流列表、TTL 与高级配置，校验内容并提示生效条件。
- 配置历史、差异、下载与单文件恢复；受控应用 custom_mod，失败时回退旧运行配置。
- 组件状态、两路 Unbound 的 QPS 与命中率、Redis 内存及持久化信息。
- DNS 查询、UDP/TCP 链路对照、诊断报告导入导出与受控 Redis 快照任务。
- 真实入口查询记录默认关闭，仅管理员可查看；来源、域名、类型筛选与分页有界，缓存命中与实际分流路径保持未知。
- Debian 原生版可在网页检查官方发行版、确认升级并查看结果；独立任务支持重启后恢复结果，默认不自动升级。
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
