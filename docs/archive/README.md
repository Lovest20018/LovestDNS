# 历史文档

本目录保留各阶段的公开方案、候选和实验记录，内容反映当时版本与条件。当前使用入口见[文档首页](../README.md)和[项目 README](../../README.md)。

## 历史安装与维护

- [Debian 原生安装、升级、备份与回退](../lovestdns-native-deployment.md)：推荐的独立部署入口。
- [Docker / PVE CT 兼容部署](../lovestdns-pve-deployment.md)：源码构建与旧数据接入边界。
- [DNS 诊断](../dns-diagnostics.md)：查询、链路比较、报告与宿主机统计。

## 开发与接口

- [query1.5 软件更新专用代理](2026-11/native-query15-release-2026-11-14.md)：原生版直连/HTTP CONNECT/SOCKS5、独立凭据与真实下载安装验收。
- [软件更新专用代理与正式版收尾](2026-11/stable-release-readiness-2026-11-14.md)：当前源码的更新网络设置、验证边界，以及中断恢复、备份、更新通道和最终候选验收条件。
- [query1.4 原生规则更新修复](2026-11/native-query14-release-2026-11-14.md)：持久化规则资产、下载回退、完整校验、失败恢复与退避重试。
- [query1.3 网页软件更新发行记录](2026-11/native-query13-release-2026-11-14.md)：原生版更新入口、独立主机任务、同包升级与失败恢复、公开下载验证。
- [query1.2 公开发行记录](2026-11/native-query12-release-2026-11-14.md)：包含六项 UI 修复的原生包、固定来源、同包验收及真实公开下载验证。
- [query1.1 主线记录](2026-11/query11-mainline-2026-11-14.md)：此前源码、采集时钟修复、六项 UI 修复及验证边界。
- [真实查询观测 MVP](2026-11/query-recording-mvp-2026-11-12.md)：默认关闭的入口采集、最近查询、容量及历史验证边界。

- [项目现状、后续交付与 UI 优化](2026-11/project-next-steps-and-ui-2026-11-11.md)：区分源码、部署和组件候选，以单设备接入、首页重排、配置验证和真实查询观测分期交付。
- [家用控制台首批实现](2026-11/home-console-implementation-2026-11-11.md)：home1 首页重排、原位配置工作流、监听采集与验证边界。
- [实例身份与安装来源实现](2026-11/home2-installation-identity-2026-11-12.md)：命名实例维护命令、应用/前端/基础包来源和发布绑定收据。
- [项目分区与发布](../project-layout.md)：各目录职责、构建依赖、GitHub 目标和发布产物。
- [开发、构建与回归](../../integrated/README.md)。
- [独立 DNS 组件构建与兼容测试](../../packaging/components/README.md)及[组件更新、性能优化方案](2026-11/component-upgrade-performance-plan-2026-11-07.md)。
- [Unbound 离线缓存兼容准备](../cachedb-offline-upgrade.md)：副本计划、冲突保留和真实新旧查询验证。
- [集成 API 说明](../integration-api.md)。
- [可选 LovestGateway 联动](../gateway-integration.md)：连接配置、观测入口和数据范围。
- [PPGW 空闲连接快照问题报告](../issues/ppgw-mihomo-empty-snapshot-2026-11-07.md)：`connections: null` 导致观测 API 502 的复现、兼容修正及回归范围。
- [集成版前端依赖](../frontend-dependencies.md)与[控制面容量边界](../control-capacity.md)。
- [v1 契约、确定性样例与摘要](../contracts/v1/README.md)。
- [交互、UI 与程序优化方案](2026-11/ui-interaction-review-2026-11-06.md)及[实施与验收记录](2026-11/optimization-implementation-2026-11-06.md)。
- [控制台改版方案（修订版）](2026-11/ui-console-plan-2026-11-07.md)：以独立 DNS 后端可视化为中心，保留组件展示、精简文案，网关联动为可选扩展。

## 验证与发布

- [query1.2 原生预发布](2026-11/native-query12-release-2026-11-14.md)：163 个载荷、35 个 UI 资源、500 次计数查询以及安装／升级／回退验证。
- [query1 原生候选验收](2026-11/query1-native-validation-2026-11-12.md)：固定来源与摘要，同包安装／升级／回退、500 次计数查询通过及功能边界，保留测试准备失败记录。
- [组件更新与新控制台阶段总结](2026-11/milestone-release-summary-2026-11-12.md)：已交付组件/UI、验收证据、预发布定位与发布前历史隐私收口。
- [home2.1 公开发行记录](2026-11/native-home21-release-2026-11-12.md)：正式预发布入口、公开来源与归档摘要、Redis 构建修复、完整 CI 和真实公开下载验证。
- [十台以内终端的家用验收](2026-11/home-use-acceptance-2026-11-11.md)及[30 分钟结果](2026-11/home-use-results-2026-11-11.md)：18,854 条查询全部正确，保留四次慢应答与发送停顿；当前重点为正确性、缓存恢复和日常试用，微秒级排名研究暂停。
- [home2.1 身份与来源验收](2026-11/home21-validation-results-2026-11-12.md)：长名称修正、同包安装/升级/回退、实际设置页与两轮独立功能观察，保留前序失败。
- [home1 原生候选验收与部署](2026-11/home1-validation-results-2026-11-11.md)：新 UI 与选定组件同包核验、隔离安装/回退、测试实例升级及 15 分钟功能观察。
- [独立 Linux 系统解析器试用](2026-11/home1-linux-client-results-2026-11-11.md)：另一台主机的五分钟 libc 解析、UDP/TCP 受控路径配对与临时规则清理；明确私有进程配置和整机接入的区别。
- [单设备试用与客户端检查](../home-single-device-trial.md)：标准端口接入、设备 DNS 恢复、A／AAAA 与 UDP／TCP 只读检查，明确现有服务预检和候选实际接入的区别。
- [验证范围与可复现入口](../verification.md)。
- [优化后的 Linux 实机验收](2026-11/linux-optimization-validation-2026-11-06.md)：真实核心、故障恢复、短时测量与依赖审计边界。
- [剩余优化与候选构建验收](2026-11/optimization-followup-2026-11-06.md)：零依赖告警、慢查询容量控制、缓存预算及安装器回退。
- [Review 三项修复](2026-11/review-fixes-2026-11-06.md)：候选保留、保存响应顺序及同页日志导航的修复与回归。
- [源码与版本收口](2026-11/source-version-closure-2026-11-06.md)：未发布测试候选、源码提交和构建产物的对应规则。
- [console4 候选收口](2026-11/console-candidate-2026-11-09.md)：固定核心组合的控制台候选、测试部署与持续观察门槛。
- [console4.1 验收记录](2026-11/console-validation-2026-11-09.md)：锁文件复现、隔离操作回归、实机升级和持续运行结果，保留被拒绝的尝试。
- [公开材料与本地记录](../publication-privacy.md)。
- [旧独立 WebUI](../standalone-webui.md)：单独仓库继续保留，本仓库只复用冻结代码。
- [组件更新与性能验证记录](2026-11/component-upgrade-performance-report-2026-11-07.md)
- [组件收尾方案](2026-11/component-closure-plan-2026-11-10.md)及[本轮结果](2026-11/component-closure-results-2026-11-10.md)：先处理缓存兼容、持久化与性能关卡，再拓展功能。
- [性能调度诊断方案](2026-11/dns-scheduling-diagnosis-plan-2026-11-10.md)及[实际结果](2026-11/dns-scheduling-diagnosis-results-2026-11-10.md)：回环、发送阶段、独立心跳和指定原版复测，保留异常样本与性能验收限制。
- [另一宿主机的性能对照方案](2026-11/component-crosshost-performance-plan-2026-11-10.md)及[本轮结果](2026-11/component-crosshost-performance-results-2026-11-10.md)：Redis 受控命中路径五轮性能筛选通过，DNSCrypt 原版尾延迟重复性未通过。
- [DNSCrypt 新版独立重复性方案](2026-11/dnscrypt-candidate-repeatability-plan-2026-11-10.md)及[补测结果](2026-11/dnscrypt-candidate-repeatability-results-2026-11-10.md)：combined 候选的 HTTP/2 直连三对自身重复性通过，尚未完成新旧性能验收。
- [Unbound 收尾方案](2026-11/unbound-completion-plan-2026-11-10.md)及[组合候选验收记录](2026-11/unbound-completion-results-2026-11-10.md)：实际历史回放、当前模板容量、性能停止结果与匹配原生包的验收范围。
- [性能与容量边界补充方案](2026-11/component-boundary-followup-plan-2026-11-10.md)及[补测结果](2026-11/component-boundary-followup-results-2026-11-10.md)：角色 CPU 分配修正后 Unbound 校准仍未通过；Redis 淘汰强度 30 达到固定容量门槛，保留其写入延迟代价与候选设置边界。
- [Unbound P99 逐请求归因](2026-11/unbound-p99-ablation-2026-11-11.md)：实时日志／内存记录对照及内核配对，旧版 A/A 第二对停止，主要慢样本定位到应答后的接收侧等待。
- [Unbound 接收侧调度归因](2026-11/unbound-receiver-scheduling-2026-11-11.md)：独立追踪与逐请求复算，60 条长应答后等待的覆盖样本中，58 条主要等待 CPU；保留误差边界及原性能验收停止结论。
- [Unbound 发生器线程分配方案](2026-11/unbound-thread-affinity-plan-2026-11-11.md)及[短测停止记录](2026-11/unbound-thread-affinity-results-2026-11-11.md)：启动错误参数检查通过，共享 CPU 短测因发送空档未达负载门槛；独立线程组及后续性能对照尚未执行。
