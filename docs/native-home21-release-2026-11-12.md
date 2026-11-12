# home2.1 组件更新与新控制台预发布记录

2026-11-12。Debian amd64 预发布版已经发布：
[LovestDNS home2.1](https://github.com/Lovest20018/LovestDNS/releases/tag/v0.1.0-dev.lovestdns-native.20261112.home2.1)。

## 最终发行资产

| 项目 | 内容 |
| --- | --- |
| 版本 | `0.1.0-dev.lovestdns-native.20261112.home2.1` |
| 应用/UI 公开提交与标签目标 | `4d1a642258ff981a5b4a1aeb17f85f693bb0ddac` |
| 源码摘要 | `61854fc2416e9a60bd6906bc17beb36d074d50969af46e9eac1736d6eb353615` |
| 原生发行包 | `lovestdns-native-debian-amd64.tar.gz`，39,260,383 字节 |
| 发行资产 | 原生包、单条 SHA-256 文件、最新 `install.py`、`README-release.md` |
| 状态 | Pre-release；默认下载继续固定 `.20261104.11` |

最终公开归档的 SHA-256：

```text
af6fb9ec65a4de98fbd9dadf99aa3a50bc96ceef0bff82e98598ce72d42dec86
```

阶段功能、组件版本与未完成范围见[阶段总结](milestone-release-summary-2026-11-12.md)。本次包含 Redis 7.2.17、Unbound 1.26.2、DNSCrypt 2.1.20 combined，保留定制 MosDNS 240822.1；控制台整合概览、配置、缓存、查询诊断、维护与日志、设置，以及实例身份、维护命令和来源信息。

## 从验收候选到公开发行

从已确认的公开基线建立干净发布历史，保留本地研究分支及原始证据。最终公开应用提交的完整历史隐私检查通过，没有强推改写远端历史。初次 CI 暴露 Redis 顶层 Makefile 对两个目标分别递归构建的竞争，已修复为同一依赖图构建，失败记录保留在[初次 CI](https://github.com/Lovest20018/LovestDNS/actions/runs/43149756786)。

修复后的[完整 CI](https://github.com/Lovest20018/LovestDNS/actions/runs/43150219116)与[历史隐私检查](https://github.com/Lovest20018/LovestDNS/actions/runs/43150219173)全部通过，覆盖后端、契约、前端构建和检查、独立组件构建、缓存与 DNSCrypt 兼容、集成容器构建和运行检查。

原验收候选的归档摘要 `4aee6943d77fff3a8ae5614c0eeefd5e86181cb46a9f74086535abfcfe24065d` 继续保留在[原验收记录](home21-validation-results-2026-11-12.md)。公开包与该候选逐文件比较：151 个载荷文件的清单与权限一致，组件、运行时、安装器、后端与 UI 资源字节保持一致；仅 5 个来源元数据文件更新。源码摘要的唯一输入变化是 Redis 独立组件构建脚本的并发修复。发行包继续复用已验收的组件，不用 CI 新构建结果替换原组件。

最终公开归档另做了隔离干净安装，核对全部载荷、API 版本和来源、UDP/TCP 监听、认证与权限、配置任务以及不匹配安装收据的拒绝行为；测试实例已停止，隔离网络已移除。期间 9 次现有服务健康探测无失败，现有服务状态保持。

发行资产全部核对 GitHub 端摘要；公开发布后使用匿名下载的发行页 `install.py`，通过显式版本的 `--download-only` 路径下载并验证真实公开归档，外层摘要和内部文件清单全部通过，安装脚本与说明也与准备资产一致。此步骤只下载和验证，没有安装到检查机器。

原候选的 434 条受控查询、升级/显式回退/坏载荷拒绝/启动失败自动恢复及 15 分钟观察，仍按原验收包解释。运行载荷逐文件一致和公开包的额外干净安装共同补充发行证据，不将原观察改记为公开归档的另一次完整长期验收。

## 选择此版本

从本次 Release 下载最新 `install.py`。既有实例升级可显式选择：

```sh
sudo python3 install.py --version 0.1.0-dev.lovestdns-native.20261112.home2.1 --instance <实例名>
```

默认实例省略 `--instance`。首次安装需要私有配置并传 `--config`；既有实例省略配置参数时保留原配置。安装、备份和回退见[部署说明](lovestdns-native-deployment.md)。不要把原候选或任何草稿包的摘要用于当前公开资产。

本次仍是家用及小规模试用的预发布。真实查询来源 IP、逐请求分流与入口延迟分布尚未交付；新旧整链性能提升、严格内存容量保证和长期稳定性尚未验收。默认下载入口保留旧版，后续根据真实终端持续使用反馈决定是否切换。
