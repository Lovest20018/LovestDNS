# 控制面容量边界

Web 使用两个 Gunicorn gthread worker，每个四个请求线程。跨 worker 的内核文件锁同时只接纳两个 DNS 诊断请求；单次链路对照仍最多四个目标。单查、对照和健康诊断共用额度，旧 API 和 v1 API 也共用，避免从另一条路由绕过限制。容量满时返回 503，v1 错误码为 `UNAVAILABLE`，附 `Retry-After: 1`。客户端应等待或由用户重新发起，不能循环自动重试。

额度文件位于实例私有 run 目录，进程终止会由内核释放锁。两个诊断请求最多产生八个目标查询线程，其他 Web 线程可继续处理配置、状态和任务读取。该限制控制同时运行的慢诊断，不承诺在任意连接洪泛下持续可用。

manager 仍使用单一维护执行器及跨进程维护锁，最多接纳八个本地 IPC 请求。配置预览/配置状态检查共用一个检查名额：**一个执行中，零排队**。重复检查快速返回忙；取得维护锁最多等待约 50 ms。任务状态和能力读取不需要这个检查名额。实际应用前仍重新计算配置指纹，预览不授权过期内容的应用。

缓存预算在每次 bootstrap 重新测量，从宿主机可用内存及当前 cgroup/祖先限制中取最小剩余容量，并扣除维护预留。cgroup v2 使用 `memory.max - memory.current`，v1 使用对应 limit/usage 文件；原生配置的 `CACHE_BUDGET_MB` 继续作为上限。可用空间不足最低缓存和预留时停止本次初始化，交由已有回退路径恢复旧运行配置。[内核文档](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)。

预算不会在后台持续缩放正在运行的缓存；这种行为会改变缓存和重启语义，当前实现选择在初始化/重启时重新测量。维护期间的动态占用和真正的 OOM 上限仍由 systemd 或容器 cgroup 约束。

可重复的慢 DNS 压力入口：

```sh
python3 scripts/test-control-capacity.py --instance validation --duration 180 --output /path/to/private-results.json
```

该脚本只接受带 `.lovestdns-test-instance` 标记的命名原生实例，在回环地址创建丢弃数据包的临时 DNS 端点，记录持续慢对照期间的状态/任务读取延迟、CPU、cgroup 内存和各进程 RSS 之和，结束后恢复原 Token API 开关。RSS 之和可能重复计算共享页面，不能等同于 cgroup 实际占用。`--allow-saturation` 用于旧版本基线记录，不断言新版本延迟目标。
