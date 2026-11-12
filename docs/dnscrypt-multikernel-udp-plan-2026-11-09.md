# DNSCrypt 双内核 UDP 队列补充验收方案

从 `c3ebbba` 继续。复用已核验的普通 upstream/combined，补 Debian 12 内核 6.1 与 Debian 13 内核 6.12 的接收队列权限检查。每套内核各执行一次 upstream/combined × NET_ADMIN 有/无 × IPv4/IPv6，共八组；沿用 128 个 256 字节积压问题、4095 字节 A/AAAA、实际能力、内核队列与丢包、答案和零回源的原检查，不运行性能计时。

只使用全新测试目录、临时 network namespace 及合成缓存，限制 CPU/内存和总运行时间，持续探测在用服务。前后核对在用服务身份、完整发布文件及配置、host resolver 和 rmem 参数；剩余内存小于 1 GiB、磁盘小于 2 GiB、服务身份变化或连续两次 DNS 失败即终止。不得安装依赖、修改宿主 sysctl 或升级在用实例。

旧内核在测试 namespace 中不显示全局 rmem 参数，工具只通过短暂的 `nsenter ... cat` 读取参考 namespace 参数，记录来源，并以 IPv4/IPv6 实际 socket 申请和 `ss` 核对值。所有 DNS 客户端、夹具和解析进程仍在新 namespace 内。只要参数视图不完整、来源不明、socket 实测不符或有未知丢包，就停止并保留证据。

预检显示 6.12 的 rmem 参数在非初始 namespace 为只读，6.1 不显示这些参数。这与上游 [6.12 源码](https://raw.githubusercontent.com/gregkh/linux/v6.12.74/net/core/sysctl_net_core.c) 将全局参数以只读方式暴露到其他 namespace、[6.1 源码](https://raw.githubusercontent.com/torvalds/linux/v6.1/net/core/sysctl_net_core.c) 仅在初始 namespace 注册全局表相符。本轮不尝试调低这些参数，也不把不同 socket 申请值冒充不同 rmem_max 实机覆盖；低上限检查仍需独立内核环境。

在两台现有授权主机各执行一次有限矩阵，沿用原缺陷对照，不通过重跑、删除慢组或放宽丢包要求得到通过。完成后检查无临时组件残留，归档实际输入、制品、资源和原输出，再提交结果。默认组件、安装入口和 PR 不参与本轮；性能、生产模板、观测开销及两小时/24 小时观察仍独立保留。
