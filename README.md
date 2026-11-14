# LovestDNS

LovestDNS 基于 [PaoPaoDNS](https://github.com/kkkgo/PaoPaoDNS)，将 DNS 解析、缓存、规则配置和运行诊断集中到一个 Web 面板，支持独立的 Debian 原生部署。

[下载 v0.1.0](https://github.com/Lovest20018/LovestDNS/releases/tag/v0.1.0) · [安装与升级](docs/lovestdns-native-deployment.md) · [发行说明](docs/releases/v0.1.0.md) · [全部文档](docs/README.md)

## 功能

- 查看 DNS 服务、组件、缓存、解析耗时和持久化状态。
- 网页编辑上游、域名分流、TTL 和高级规则，提供历史记录、差异及恢复。
- 运行 DNS 查询和 UDP/TCP 链路对照，导入或导出诊断报告。
- 按需开启查询记录，按来源 IP、域名、类型筛选；数据有时间和容量上限。
- 网页检查、确认并安装软件更新，支持软件更新专用 HTTP/SOCKS5 代理。
- 正式版更新通道默认开启，预览版需主动选择；默认不自动安装更新。
- 可选 LovestGateway 联动与 `/api/v1/` 外部接入；独立部署时功能完整。

## 安装

正式发行包支持 **Debian 12 / 13、amd64、systemd**，运行时无需 Docker。其他架构暂不提供发行包。

```sh
curl -fL https://github.com/Lovest20018/LovestDNS/releases/download/v0.1.0/install.py -o install.py
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/.env.native.example -o lovestdns-install.env
chmod 600 lovestdns-install.env
nano lovestdns-install.env
sudo python3 install.py --version 0.1.0 --config ./lovestdns-install.env
sudo lovestdnsctl health
```

默认 DNS 监听 `0.0.0.0:53/UDP+TCP`，网页监听 `127.0.0.1:8123`。需要局域网访问时，在配置文件中把 `WEB_BIND_ADDRESS` 设为主机内网 IP。首次登录使用安装时生成的管理员密码，登录后必须修改；主机可用 `sudo lovestdnsctl password` 重设。

安装器核对归档及包内文件摘要。新装遇到端口占用会退出，保留已有服务；系统依赖通过 Debian 软件源安装，Python 依赖随包离线提供。命名实例、离线包、旧数据迁移和回退步骤见[原生部署说明](docs/lovestdns-native-deployment.md)。

## 升级与更新代理

在 **设置 → 服务信息 → 软件更新** 检查并确认升级。安装前备份现有配置与数据，DNS 和网页会短暂重启；失败时尝试恢复原版，关闭网页不会取消任务。

在 **更新网络与通道** 选择正式版或预览版，配置直连、HTTP CONNECT 或 SOCKS5。该代理只用于版本信息、校验文件和软件安装包，SOCKS5 由代理解析目标域名；DNS 分流和规则更新继续使用各自配置。

旧版本无法直连 GitHub 时，可先传入已校验的安装包升级一次，再配置更新代理。主机升级保留实际实例名：

```sh
sudo python3 install.py --version 0.1.0 --instance <实例名>
```

默认实例省略 `--instance`。摘要以同一 Release 附件为准，旧版本与新版本的摘要不能混用。

## 使用范围

- v0.1.0 面向小规模家庭与局域网 DNS。Docker 提供[源码构建兼容方案](docs/lovestdns-pve-deployment.md)，当前没有公开预构建镜像。
- 查询记录默认关闭；逐请求缓存命中、实际分流路径缺少证据时显示未知。
- 软件更新需用户确认。升级备份由主机管理，应保留可用回退点和足够磁盘空间。
- 正常失败恢复与回退已验证；强制终止、断电各阶段及长期负载仍有[已知限制](docs/roadmap.md)，不宣称全面性能提升。

## 开发与项目结构

| 目录 | 用途 |
| --- | --- |
| `integrated/` | 当前 Web 前端、API、观测与维护运行时 |
| `packaging/native/` | Debian 安装、升级、回退和原生适配 |
| `packaging/components/` | 固定组件来源、补丁与构建 |
| `scripts/` | 构建、回归、契约导出与隔离验收 |
| `docs/` | 当前使用和开发文档；历史方案集中在 `docs/archive/` |
| `web/`、`packaging/legacy/` | 冻结的旧独立 WebUI 与兼容材料 |

[构建和测试](integrated/README.md) · [API 文档](docs/integration-api.md) · [验证范围](docs/verification.md) · [后续工作](docs/roadmap.md)

本地构建产物、真实配置、凭据、数据库和现场记录不进入公开源码。旧 WebUI 保留于独立的 [PaoPaoDNS-WebUI](https://github.com/Lovest20018/PaoPaoDNS-WebUI) 仓库；本仓库使用 [GNU GPLv3 许可](LICENSE)，保留上游归属与固定源码记录。
