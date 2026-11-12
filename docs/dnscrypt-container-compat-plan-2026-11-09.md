# DNSCrypt Debian 12 用户态与容器兼容方案

从 `fd0140b` 继续。41 原条件下的定速停顿已有记录，不重复计时以取得通过。本轮使用已有、按 image ID 固定的 Debian 12 夹具镜像与 Alpine 项目镜像，只检查功能兼容；默认组件锁、部署、在用数据和正式机不改变。

1. Debian 12 容器使用 `network=none`、零端口发布、只读输入挂载、新工作目录和资源限制。只读挂载宿主机 network namespace 作为隔离参照，避免把容器自己的 PID 1 误当宿主机。用既有普通 upstream/combined、当前夹具和同一旧 MosDNS/Unbound/Redis 运行全部 30 组协议、缓存、TTL 与受控分流功能检查。记录用户态和共享宿主内核；不称为 Debian 12 原生 systemd 整包安装验收。
2. 基于同一个已存在的旧 Alpine 项目 image ID，构建两个显式测试镜像：共同更新当前 runtime/backend/app，保留旧前端、依赖与其他组件；combined 仅增加已验收普通 DNSCrypt、源码锁、来源清单和相应构建元数据。核对真实二进制摘要及冻结 MosDNS，全部标为候选、未批准发布。
3. 扩展既有容器 smoke 工具支持离线 HTTP、升级镜像与回到 control。使用新建测试 volumes，经实际 entrypoint/supervisor 启动；校验 API、UDP/TCP DNS、配置保留、RDB 标记、身份保留、配置应用/失败恢复及观测进程断开恢复。结束停止并移除测试容器和 volumes。RDB 标记保留不代替实际 DNS 缓存命中证明，后者沿用独立专项结论。
4. 原始失败、输入、image ID、文件摘要和日志全部保留。主机资源或在用服务异常即停止；记录 CPU 限流与内存事件，不把受限功能运行变为性能证据。结束再次核对在用 PID、重启数、发布目录、组件、resolver 与 sysctl。

本轮不覆盖 Debian 12 原生安装/回退、多内核、生产规则与真实历史缓存全集、网关联动开销、最大吞吐、两小时负载或 24 小时观察。性能验收门槛保持原样，等待稳定环境。
