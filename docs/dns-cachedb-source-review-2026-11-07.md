# Unbound / Redis 源码与阻塞路径核查

日期：2026-11-07。关联 [丢包原因定位](dns-benchmark-loss-diagnosis-2026-11-07.md)。本轮检查官方源码、发行记录与公开讨论，并进行独立故障注入；没有修改 41 的运行实例。

## 结论

**Unbound 的 Redis cachedb 同步访问确实存在拖慢其他 DNS 请求的路径，已复现；目前仍不能把原始 mixed90 丢包判定为新版 Redis 或 Unbound 回归。** 两类证据需要同时保留：Redis 卡顿可以阻塞同一 Unbound 工作线程；此前的追踪也记录到了发生在 UDP `sendto` 内部、伴随跨 CPU 心跳延后的停顿。

## 官方源码确认了什么

检查范围：Unbound 1.21.1 / 1.26.2、Redis 7.2.5 / 7.2.17，以及候选 Unbound 实际链接的外部 hiredis 1.3.0。旧版官方源码用于比较逻辑，不能据此断言旧二进制没有额外补丁。没有用 Redis 源码包自带的 hiredis 代替候选依赖。

- Unbound `cachedb/redis.c` 的 `redis_command()` 按工作线程选择连接，再同步调用 `redisCommand()`。Redis 慢时，该工作线程连内存缓存命中的新请求也不能及时处理；多线程时不应直接推断所有线程都已阻塞。这是 [官方手册明确说明的限制](https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html#cache-db-module-options)。
- 新版 `redis-command-timeout`、`redis-connect-timeout` 默认均回退到 `redis-timeout: 100`。冷查询可能依次进行 GET、上游解析、SET，因此可承受两次缓存等待。hiredis 把等待设置为 socket 收发超时，并非整条 DNS 请求的总时限；设置 20ms 不代表 DNS 必定在 20ms 内完成。参见 [hiredis 1.3.0 网络实现](https://github.com/redis/hiredis/blob/v1.3.0/net.c)。
- 新版重连节流累计的是连接及初始化失败，成功建立并初始化连接会清零计数；已建立连接后的 GET/SET 返回空指针，只关闭连接，不增加该失败计数。因此“连接可建立、命令一直无响应”并不一定触发节流。内部等待间隔为 1 秒，没有 `redis-reconnect-interval` 配置项。该判断来自 [`redis_connect()` / `redis_command()` 实现](https://github.com/NLnetLabs/unbound/blob/release-1.26.2/cachedb/redis.c)，属于代码分析，不能扩大成所有故障模式都绕过节流。

特别是 DB 1 重连时需要 SELECT，启用认证还需要 AUTH；这些初始化步骤超时会进入连接失败计数。下面的 DB 0、无认证实验不覆盖 DB 1 的该行为。

官方邮件列表在 2025 年已讨论 [Redis 异常、逐查询等待和重连退避](https://lists.nlnetlabs.nl/pipermail/unbound-users/2025-February/008482.html)。候选发行包的 `doc/Changelog` 包含 2025-07-24 的重连节流及 2025-07-28 的相关初始化修正；本轮不是发现了需要补装这两项修复。

## 独立实验

使用新网络命名空间、受控 DNS 上游、独立 Redis Unix socket、单线程 Unbound、DB 0、TTL 600 秒。先预热一个域名，仅暂停实验 Redis；发送一个冷查询，约 10ms 后发送预热查询。每组重复三次，每次均在 `finally` 中恢复 Redis。

下表为中位数，单位 ms：

| 组合 | 冷查询 | 随后到达的内存热查询 | Redis 恢复后的热查询 |
|---|---:|---:|---:|
| 原版本 | 208.472 | 92.715 | 0.783 |
| 组合候选，默认参数 | 207.473 | 91.796 | 0.773 |
| 组合候选，命令超时 20ms | 48.555 | 12.088 | 0.918 |

九次实验全部返回正确答案，每次上游仅新增一次请求，符合只有冷查询需要上游的预期。热查询不必等冷查询的 GET、SET 全部完成，因此不能把其延迟简单估计为冷查询耗时减 10ms。

这证明同步缓存访问的影响，也说明缩短命令超时有故障场景收益；它不能证明正常解析提速，或证明此前丢包时 Redis 正在发生同样的阻塞。20ms 保持实验参数。

## Redis 版本差异及其他路径

逐文件比较 Redis 官方源码：`t_string.c`（GET/SET）、`ae.c`、`ae_epoll.c`、`expire.c`、`dict.c`、`zmalloc.c`、`latency.c` 在两个版本间相同。网络及服务端代码有客户端内存统计、协议安全、模块线程和 accept 错误处理等修正；不能因为部分文件相同就推断整个运行行为相同。已查 [7.2.17 发行记录](https://github.com/redis/redis/blob/7.2.17/00-RELEASENOTES)，尚未找到与现有丢包证据吻合的普通 GET/SET 延迟修复。此范围不排除编译选项、依赖或未覆盖代码中的问题。

两轮定向丢包实验的 Redis INFO 都显示：`evicted_keys=0`、`expired_keys=0`、`total_forks=0`，AOF 关闭，使用内存约 4 MB、上限 64 MiB。此次实验没有触发淘汰、过期清理或持久化 fork 的证据，不能将这些常见原因直接套入本次失败。

Unbound 的 UDP 发送也有拥堵时等待 `poll()` 的分支，但此前约 188ms 的停顿位于单次 `sendto` 系统调用内部，没有该分支触发的证据。不能因等待常数接近就认定是这段重试逻辑，更不能把它视为 hiredis 读取阻塞。

另按 [Redis 官方延迟诊断方法](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/#latency-baseline)，在测试机 CPU 10 执行一次 100 秒 `redis-cli --intrinsic-latency 100`，最大间隔为 **2.940ms**。该模式不连接 Redis，也不依赖 VMware 统计接口；这次没有捕获此前的大停顿，不能据此排除间歇性环境问题。

## 后续处理

1. 保留同步阻塞为已确认的架构限制，对超时参数继续验证健康路径、Redis 命中率及持续故障行为，再决定默认值。
2. 若需要改善“Redis 可连接但处理停滞”，优先评估命令失败后的短期旁路及其恢复机制；须区分 DB 0、DB 1、认证和多线程，先形成可复现用例，再决定是否提交上游或维护补丁。当前未修改组件源码。
3. mixed90 的发送突发、服务端队列溢出和公共停顿仍需按原定位文档继续验证，不能用此次缓存故障实验补签性能通过。

原始九次测量、57 个源码文件摘要、17 个 Redis 文件差异及官方诊断输出保存在私有证据目录。汇总证据 SHA-256：`9dbe095e110258264cc01ed4562e08edfd265a45f64ae8d5a8ac119304d58a62`。实验结束后独立进程已退出；41 当前服务保持 active，`NRestarts=0`。
