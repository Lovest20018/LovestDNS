# DNSCrypt Debian 12 用户态与容器兼容结果

从 `fd0140b` 继续，执行 [本轮方案](dnscrypt-container-compat-plan-2026-11-09.md)。**Debian 12 用户态的 30 组功能检查，以及 Alpine 项目容器的升级、配置应用、故障恢复和切回旧镜像均通过。** 同时定位并修复了配置维护任务与自动重载之间的争锁问题。默认组件、在用实例、正式机及 PR 没有改变；本轮不签署性能通过。

## 实际环境与制品

Debian 12 夹具镜像按现有完整 image ID 固定，实际为 glibc 2.36、Python 3.11.2。项目容器以现有旧 Alpine 镜像为基础，其 `/etc/os-release` 标记为 `3.21.0_alpha20240807`、Python 为 3.14.9。**两者共享 41 的 `6.12.74+deb13+1-amd64` 内核**，不是两套内核或 Debian 12 原生 systemd 整包安装验收。

Debian 12 功能容器使用 `network=none`、只读工具/候选/旧核心输入、新工作目录及只读宿主 namespace 参照，不发布端口。项目容器与专用的 `network=none` TLS DoH / 实际 SOCKS 夹具共享隔离网络，HTTP 通过 `docker exec` 访问；测试全部使用新建合成数据，不访问生产 Redis 或回放生产域名。

`build-dnscrypt-container-trial.py` 从同一个旧组件镜像生成匹配的 control / combined：两者共同覆盖当前 app/runtime/backend，保留原前端、Python 依赖及其他核心；combined 另放入普通 DNSCrypt、来源清单、源码锁及元数据。核对实际 MosDNS、Unbound、Redis 与原镜像字节一致，DNSCrypt 与各自预期摘要一致；镜像层审计也确认基线层保留、应用层共用，后续层仅对应声明的差异。

| 制品 | 身份 |
|---|---|
| 旧 Alpine 基础镜像 ID | `sha256:073757bc11a1f2b649ceb29a777f06585c26f96126d97cd90c9a6d4b808e1f35` |
| 最终 control 镜像 ID | `sha256:7947cfcd353a816ec528dc16340fd2a8d6a3b1c3b5ab61ea154fe0cd1f23f1c2` |
| 最终 combined 镜像 ID | `sha256:6cea2b4a939d25b438cd6577dbf1dee9266c69db2518bc2986cd80ad05376e9e` |
| combined 普通 DNSCrypt SHA-256 | `f7b80a185b7c1e4ae58615e9225d5af6233f81523569237fbcd2cbd8d3c3a2c4` |
| 匹配镜像导出压缩包 SHA-256 | `1cd702bb0c1c3263fee426a5db54bb07d04f728b44f432c36af84deb8918d388` |
| 私有完整证据 ZIP SHA-256 | `d07665ccaf77fb7645b7f94745a9b839e16a5d128e396b0389bf21a0f5fbec01` |

所有镜像/结果保留候选及未批准发布标记。没有修改默认 Dockerfile、默认组件锁或 DNSCrypt 源码锁。最后一次完整容器复核复用第 4 套输入所建镜像；第 5 套输入逐项核对其应用、源码锁及候选字节与构建清单一致，没有把更新过的测试工具混作新组件。

## 功能覆盖

Debian 12 运行既有普通 upstream / combined，使用同一旧定制 MosDNS、Unbound、Redis 及私有依赖。**30 组、13,574 次被计数查询全部通过**，初始化及就绪探针另计。

| 检查 | 实际覆盖 |
|---|---|
| TLS 与路由 | HTTP/1.1 / HTTP/2、ALPN、直连 / 实际 SOCKS |
| DNSCrypt 缓存 | 容量 1/17/512、同名 A/AAAA、大小写、淘汰、8 客户端并发 |
| 过期刷新 | 两组短 TTL，首次回源、立即命中、失效后再次回源 |
| 冻结 MosDNS 分流 | 四组各 64 次冷查询、64 次热查询，核对本地问题账本、DoH 和 Redis 计数；热阶段阻断后端 |

Alpine 的完整流程由扩展后的 `test-integrated-container.py` 执行：先以 control 新装、`CNAUTO=no` 启动，检查 API 角色、认证、数据新鲜度、UDP/TCP、本地记录及配置历史恢复；正常停止后用同一 data/state volumes 切到 combined、`CNAUTO=yes`，确认两层 Unbound、实例 ID、原自定义值与 Redis RDB 标记保留。

候选实际应用一条 Hosts 规则并通过目标 DNS 查询验证，重复提交保持同一任务；随后仅在该测试容器内拒绝指定候选的 MosDNS 启动，确认运行配置回退、期望文件保留及旧规则 DNS 恢复。Redis 后台保存、观测进程断开后的 503/UNAVAILABLE 与恢复、DNS 核心停止后的不健康状态及 Web 进程重启后查询也通过。最后正常停止 combined 并切回 control，实例 ID、原配置值、RDB 标记和 DNS 仍保留。

升级后及返回 control 后，各通过两路 DNSCrypt 的 A/AAAA、先 UDP 冷查再 TCP 复查，共 **16 次查询**；每阶段 HTTP/2 DoH 增量为 direct 2、proxy 2，ALPN h2 一致，新增拒绝及 DNS 错误均为零。检查了两条实际运行进程的 `/proc/*/exe`；combined 摘要与上表一致，control 恢复旧二进制。候选两份生成 TOML 均保留 `pqdnscrypt=false`、`lb_strategy='p2'`。

这里的容器 RDB 标记检查证明数据保留，**不代替全部 DNS 缓存条目首次命中的证明**；后者仍分别参考 [原生整包结果](dnscrypt-native-trial-results-2026-11-09.md) 及历史缓存专项。切回旧镜像是显式重建容器，不称为容器升级失败自动回退。

## 新发现的维护争锁及修复

在第 2 套容器流程中，已接受的 `apply_custom_mod` 失败，仅返回通用错误。原工具清理前没有保存完整组件日志；该次任务输出仍保留，不能补造缺失现场。第 3 套补充意外异常日志及私有现场复制后复现，堆栈明确为 `maintenance_lock()` 的 `BEGIN IMMEDIATE` 遇到 `database is locked`，两秒后抛出 `MaintenanceBusy`。自动 watcher 正在执行 MosDNS 重载；失败发生在管理任务更改运行配置之前，不是 DNSCrypt 查询处理异常。

配置应用 worker 现在有界等待最多 60 秒，覆盖既有自动重载子进程的 45 秒时限；预览和普通配置写入继续使用原短等待。锁最终仍不可用时，任务返回明确的维护忙提示；其他意外异常写入管理日志，接口继续保留通用错误。取得锁后仍重新核对预览摘要，没有跳过过期预览、互斥或回退检查。修复只作用于配置维护任务，尚未部署到在用实例。

新增真实 SQLite 锁占用超过旧两秒门槛后仍成功应用的回归，以及忙提示/内部异常日志检查。修复后的第 4/5 套流程均通过真实配置应用及目标记录查询；第 5 套还完整通过收尾。最后一套 **212 项 Linux 定向回归通过**，包括组件工具、原生包/重启、抓包、配置编译/状态及维护任务。

## 准备与工具失败的保留

| 输入 | 原始结果及后续处理 |
|---|---|
| 第 1 套 | Debian 12 的 30 组通过。构建入口把裸 `sha256:ID` 放进 FROM，BuildKit 将其解释为仓库/tag，元数据访问超时；项目容器尚未启动。改为核对完整 ID 后创建全新本地标签，再以 `--pull=false` 构建并前后核对标签身份。 |
| 第 2 套 | 匹配镜像、启动、身份/RDB 保留及候选加密路径通过，随后配置任务失败；保留任务输出。 |
| 第 3 套 | 补齐日志与合成现场保存，明确复现维护争锁；原失败不改判。 |
| 第 4 套 | 修复争锁，配置应用、故障恢复、保存和观测恢复通过；最后旧镜像协议检查因累计计数断言失败。 |
| 第 5 套 | 修正阶段计数后完整通过；不重跑原 Debian 12 功能矩阵或重建已核对的镜像。 |

第 4 套工具错误地要求跨阶段累计夹具 DNS 错误一直为零。Go 夹具只支持受控 `.cache.test` 等问题，管理面 `.invalid` 健康探测会被夹具拒绝；该管理探针本来只证明 DNS 协议响应，目标规则另有实际查询验证。修正后保存每阶段 before/after 及增量，不清零历史计数。第 5 套返回 control 阶段实际为 **2 → 2**，本阶段错误增量为零；4 次回源和两路协议均匹配。新增反例确保新的错误、错误数量、错路由或错 ALPN 仍拒绝通过，不用累计检查的修正掩盖当前错误。

## 收尾与边界

五次任务的 **70 次在用服务健康探测全部成功**，最低可用内存约 5856 MiB，归档后根分区约 3.93 GiB 可用。主要测试任务使用 CPU 4–7 及内存/时间上限；这些受限功能运行不提供性能结论。所有临时容器与带测试标记的数据卷均已移除。41 原在用服务仍为 PID 1047426、NRestarts=0、active；发布目录、核心及私有依赖摘要、host resolver 和 rmem 参数与开始前一致。

本地私有 `.build/dnscrypt-compat-evidence-20261109.zip` 约 93 MB，包含五次输入/原日志、30 组结果、失败现场、最终合成数据/状态、协议计数及最终两镜像导出。镜像导出已核验 35 个 OCI blob 摘要；证据索引共 1086 个文件，排除 TLS 私钥与链接目标。证据按各次实际输入解释，不将准备失败改判为通过。

后续仍需 Debian 12 原生 systemd 整包安装/升级/回退、更多生产配置和多内核验证、网关观测开销、两小时负载及 24 小时观察。严格性能验收继续等待能稳定定速的环境，保留原门槛及此前停顿失败记录；本轮不重复同条件计时直到碰巧通过。MosDNS 继续冻结，Redis / Unbound 的既有更新与历史缓存边界不由本轮改变。

随后已完成 [Debian 12 实际宿主机原生验收](dnscrypt-debian12-native-results-2026-11-09.md)，补足该原生安装、升级、缓存复用及两种恢复路径；性能和持续观察仍未通过。全部维护进展见 [详细更新说明](component-maintenance-update-notes-2026-11-09.md)。
