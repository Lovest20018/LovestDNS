# 基于 PaoPaoDNS 源码的功能增强与双版本划分

日期：2026-10-24。本文是源码分析和实施方案，不代表相关增强已实现。

## 1. 核查对象与结论

本次成功取得 GitHub 源码归档，并读取了 PaoPaoDNS 使用的定制 MosDNS，而非套用普通 MosDNS 的接口文档。

- PaoPaoDNS：`a8da2e62a73026c9594b7edc36447538e51000ee`。在线归档中的 11 个关键文件与本机 `<local-source-directory>` 一致。
- kkkgo/mosdns：`7a92c687171e118414a5cc9e77f0d0da8f2a9d76`。
- 归档提交、SHA-256 和文件核对结果：[核查记录](research/upstream-source-review.json)。

**建议两个版本具有不同功能定位：独立版做兼容上游的配置和诊断工具，集成版做带可控核心的 DNS 服务。** 共用前端基础组件、配置模型与诊断协议；集成版可增加自身配置模板、管理服务，以及小范围、可追踪的 MosDNS 补丁。无需为了功能一致而限制集成版，也无需把纯配置功能从独立版拿掉。

本次没有运行或改动 DNS 服务，没有构建镜像。上游预构建流程会拉取浮动依赖，因此源码提交不能单独证明某个已发布镜像内二进制的行为；发布前仍要锁定镜像/组件版本并验证。

## 2. 源码实际结构

在启用 CNAUTO 的常见配置下，主要路径如下；各分支有缓存、并发和回退，不能理解为每次请求依次经过全部组件。

```text
客户端 → MosDNS :53（规则、IPv6 策略、前端缓存）
             ├─ 本地递归 → Unbound :5301 → 根/权威 DNS
             │                └─ Redis DB 0 持久化缓存
             ├─ 加密转发 → Unbound :5304 → DNSCrypt :5302 / SOCKS :5303
             │                └─ Redis DB 1 持久化缓存
             ├─ 直接调用 DNSCrypt :5302 / SOCKS :5303（按分支）
             └─ 自定义转发、Zones、Hosts、Swaps
```

依据：[初始化脚本](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/init.sh)、[MosDNS 模板](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/mosdns.yaml)、[Unbound 模板](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/unbound.conf)。关闭 CNAUTO 时拓扑不同，页面必须按实际运行配置展示。

## 3. 最值得做的功能

### 3.1 解析路径解释：优先级最高，两版都能受益

源码已经有 `ADDINFO`，会把来源、累计耗时等信息写入 DNS 应答 Additional 区的 TXT 记录。当前 WebUI 的 `_parse_dns_response` 只解析 Answer 内容，Additional 只返回数量，所以这部分诊断信息尚未展示。

建议在查询结果中显示：是否命中前端缓存、走递归还是加密转发、是否被 IPv6 策略阻止，以及实际返回的调试标记。扩展 NS、MX、TXT、SRV、PTR、HTTPS 等查询类型，并显示真实 RCODE，方便排查内网域名、邮件和服务发现。

- 独立版：读取核心已经返回的 ADDINFO，提供启用说明、配置依赖提示和离线规则检查。
- 集成版：增加带期限的诊断模式，受控应用配置；后续用内部事件接口记录本次请求路径，避免向所有客户端返回调试信息。
- 不把 ADDINFO 当完整调用跟踪：它只覆盖执行到的标记，耗时是累计值；缓存会保留非 OPT 的 Additional 记录，旧标记可能属于缓存生成时的查询。页面需要区分本次缓存命中与历史来源，无法区分时明确说明。
- 报文 RCODE 优先于调试文案：定制核心存在写入 NXDOMAIN、但调试文本称 REFUSED 的分支，不能按文本反推状态。

依据：[addinfo.go](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/addinfo/addinfo.go)、[缓存复制逻辑](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/cache/utils.go)、[应答处理](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/pkg/server/dns_handler/entry_handler.go)。

### 3.2 配置策略工作台：完善已有功能

当前 WebUI 已有 Zones、Swaps、Hosts 可视化表单，不能把这些重复算作新增功能。需要补齐的是完整使用流程：

- 场景模板：公司域转发到 AD DNS；NAS 域名映射内网 IP；指定 CIDR 的解析结果替换；特定域名走旁路网关。
- 自动关联 `custom_mod.yaml` 与 `custom_env.ini` 的 `env_key`，检查缺失变量和非法地址。
- 管理被引用的域名/CIDR 文件。当前后端只允许固定文件，无法从页面创建任意 `test_cidr.txt`；需新增受限目录和文件类型，不开放任意路径读写。
- 检查重叠规则、缺失依赖、记录类型限制；支持配置差异、历史版本及恢复。
- 同时支持 Swaps 的多文件形态。上游 Go 类型是字符串数组，当前表单把 `cidr_file` 转成单个字符串，应补齐无损转换和往返验证。

独立版可以提供上述配置工具，但不能自动确认核心已加载。集成版增加“生成预览 → 校验 → 应用 → 运行验证”。

另一个源码限制：`AddMod` 用 Go map 组织生成结果，而且部分分组键没有包含 `seq`。因此不能仅按 YAML 行顺序承诺规则优先级；重叠分组要告警，集成版应修正分组和稳定排序，并验证输出语义。

依据：[AddMod 配置生成器](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/coremain/addmod.go)，本项目 `web/src/pages/AdvancedConfigPage.tsx`、`web/backend/app.py`。

### 3.3 实时看板：集成版第一批

可以直接接入的基础：

- 递归侧 Unbound 已启用 `/tmp/uc_raw.sock`，可设计只读统计采集。
- Redis 使用 `/tmp/redis.sock`，具有 DB 0/1，可采集内存、键数量、持久化及加载状态。
- 组件进程、实际配置文件、内部端口均可在集成容器内检查。

必须新增的部分：

- 转发侧 Unbound 明确配置 `control-enable: no`。需为它增加独立控制 socket，不能直接复用递归 socket。
- 定制 MosDNS 的 `metrics_collector` 导入被注释，缓存实现没有现成命中计数/API；当前模板又使用内联 `cache`。要展示入口总 QPS、前端缓存命中率和分流计数，需要补充计数器及只读出口。
- 两个 Unbound 的统计只能代表各自处理的请求；不能相加当作客户端总请求数，也不能把 Redis 命中率当整条 DNS 链路的缓存命中率。

建议首屏展示“入口请求、失败与延迟、各层缓存、各组件健康”，指标标注来源和观察窗口。采集缺失显示不可用，不显示为 0。

依据：[控制接口配置](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/unbound.conf#L1058)、[Redis 配置](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/redis.conf)、[插件注册](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/enabled_plugins.go#L80)、[缓存实现](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/cache/cache.go)。

### 3.4 查询日志与排行：集成版第二批，有现成基础

定制 MosDNS 已编译 `query_summary`，查询上下文能输出客户端地址、域名、类型、RCODE、耗时和错误。但当前模板没有调用该插件，日志级别为 error，而查询摘要写在 Info 级别，因此目前并不存在可直接展示的完整查询日志。

实现方式：在缓存之前包裹整个请求链的摘要记录，验证缓存命中、正常响应和错误响应均被覆盖；增加限量采集、轮转/保留期限与开关。需要精确缓存层、规则命中、分流原因时，再补充结构化字段，不能从现有摘要凭空推断。

用户价值：按域名、客户端、失败码筛选；慢查询排行；最近失败；从一条查询进入规则检查。默认关闭持久化或提供短期诊断模式，统计与日志可以分别启用。

客户端身份以核心实际看到的来源为准。路由器转发或 NAT 后可能只有网关地址，不承诺自动识别每台设备、主机名或 MAC。

依据：[query_summary](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/query_summary/query_summary.go)、[摘要字段](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/pkg/query_context/context.go#L163)。

### 3.5 域名拦截与设备分组：集成版的主要功能差异

已有基础是 `domain_set`、`qname`、`client_ip`、`black_hole` 和序列拒绝动作；上游的 `custom_mod.yaml` 没有提供完整客户端策略或订阅管理结构。

建议做成集成版独有的策略层：

- 域名黑白名单、受支持格式的拦截订阅、命中原因和临时放行。
- 按客户端 IP/CIDR 分组，选择是否过滤、递归/转发策略及内网解析覆盖。
- 配置化处理 AAAA、HTTPS/SVCB。当前模板直接拦截 qtype 64/65，集成版可以提供明确的兼容选项和验证结果。

必须先解决缓存语义：当前缓存位于规则入口之前，缓存键不含客户端或策略组。单纯在后面添加客户端规则，会让缓存命中绕过策略，或让不同组复用不适用的结果。应先识别组并执行必要的访问/过滤判断，再进入按策略隔离的缓存；策略更新时处理旧缓存。涉及不同上游的下层缓存也需评估隔离范围。

首期只支持明确的域名规则格式，不把完整浏览器广告规则直接当域名列表。DNS 拦截只针对域名，不能承诺去除与正文同域提供的广告。

依据：[已注册插件](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/enabled_plugins.go)、[拒绝动作](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/sequence/built_in.go)、[缓存键](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/executable/cache/utils.go#L43)、[请求顺序](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/mosdns.yaml)。

### 3.6 缓存维护与数据更新中心：集成版第二批

上游 `flushd_server` 已通过递归侧 socket 执行 `flush +c <domain>`，但它是延迟查询 A 记录、发现 TTL 为 0 后处理的特定流程，不是完整多层缓存清理 API。

建议提供按层查看、指定域名失效、限速的选定域名预热，并返回逐层结果。MosDNS、两个 Unbound、Redis cachedb 与 DNSCrypt 的清理接口和覆盖范围都要验证；未支持的层明确标记。不能只清一个层就提示“全部刷新”，也不能把 Redis 键当可读域名直接删除。

数据更新中心可以管理根提示、IP 库、全球域名标记库和 Tracker 数据的版本、校验和、上次更新结果与重试。上游 `data_update.sh` 包含随机等待、等待文件监视器和升级 CA 包的操作，直接接到网页同步请求上会产生不可控等待。应拆成有超时、锁、进度和独立结果的后台任务；镜像内软件依赖与数据更新分离。

依据：[flushd_server](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/server/flushd_server/flushd_server.go)、[data_update.sh](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/data_update.sh)。

## 4. 集成版需要先补的基础能力

| 源码发现 | 对产品的影响 | 建议改造 |
|---|---|---|
| `init.sh` 启动多个后台进程后 `tail -f /dev/null` | 容器存活不等于 DNS 健康 | 明确进程监督、准备状态、重启退避和退出信号 |
| 多项变量在 `init.sh` 用 sed 生成配置；监视器仅重启 MosDNS | 修改环境变量不等于全部重新生成并生效 | 按变量定义应用范围，拆分生成、校验、加载步骤 |
| `watch_list.sh` 在 CNAUTO 关闭时不监听这些分流配置 | 当前 WebUI 的“无条件自动重载”提示过宽 | 两版修正为版本/运行模式相关提示 |
| `reload_dns force` 内部又将强制标记置零，依赖后续变化判断 | 不能把脚本名当作无条件重载保证 | 集成版提供明确的受控应用动作并验证 |
| AddMod 解析失败时打印后返回，旧输出文件可能仍存在 | 退出码/文件存在不足以判断生成成功 | 返回明确错误、清理本次中间产物、按配置版本应用 |
| AddMod 的 map 分组与顺序行为 | 重叠规则可能不符合页面直觉 | 稳定生成顺序、分组键纳入语义、冲突检查 |
| 从 `/proc/meminfo` 分配缓存，未见 cgroup 限额处理 | 配置容器内存限制后可能预算过大 | 使用容器可用预算，预留 WebUI/采集器内存 |
| 启动时首次镜像运行会清理一批 `/data` 文件 | 自定义生成内容可能在重建时丢失 | 持久用户输入与生成文件分离，明确升级迁移 |
| HTTP_FILE 提供整个 `/data` 的目录浏览；入口递归 chmod 777 | 不适合存放面板日志、凭据与备份 | 面板状态独立卷，下载走鉴权和白名单 |
| debug/trace 脚本包含杀进程与修改配置 | 不能视为普通只读诊断按钮 | 把只读采集与有副作用诊断分开实现 |

这些是源码审阅发现；涉及实际故障触发条件的项应在隔离容器中复现后再修复，不直接修改运行实例。

依据：[init.sh](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/init.sh)、[watch_list.sh](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/watch_list.sh)、[reload.sh](https://github.com/kkkgo/PaoPaoDNS/blob/a8da2e62a73026c9594b7edc36447538e51000ee/src/reload.sh)、[AddMod](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/coremain/addmod.go)、[HTTP 文件服务](https://github.com/kkkgo/mosdns/blob/7a92c687171e118414a5cc9e77f0d0da8f2a9d76/plugin/server/httpd_server/httpd_server.go)。

## 5. 调整后的双版本边界

| 能力 | WebUI 独立版 | PaoPaoDNS 集成增强版 |
|---|---|---|
| 上游文件与配置模型 | 兼容，不接管核心 | 兼容基础配置，另有增强策略模型 |
| Zones/Hosts/Swaps | 表单完善、关联校验、文件管理 | 同左，加生成校验与受控应用 |
| DNS 诊断 | 网络查询、ADDINFO 解析、离线检查 | 同左，加内部链路与运行状态 |
| 备份恢复 | 配置备份与恢复、生效提示 | 同左，加应用结果与回退验证 |
| 实时统计 | 离线报告；不伪装为在线状态 | 核心指标、趋势、组件健康 |
| 查询日志与排行 | 不承诺读取不可访问的核心日志 | 可选采集、检索、来源与耗时分析 |
| 域名拦截/设备分组 | 不提供需改核心的策略执行功能 | 增强策略与缓存隔离 |
| 缓存维护/数据更新 | 显示可获得的信息与操作说明 | 受限后台任务与结果反馈 |
| 核心维护 | 用户原有方式 | 本项目验证并维护的小范围补丁 |

策略执行差异由实际运行能力决定。将来独立 WebUI 若连接兼容的受限管理服务，可以复用增强页面；不依赖镜像名称硬编码能力，也不把无接口的按钮留给用户试错。

集成版新增策略单独存储，例如面板状态卷中的 `policies.yaml`，再生成核心配置。回退到官方核心时导出兼容部分，并明确列出不能迁移的设备策略/拦截设置，不能承诺两个版本所有数据完全互换。

## 6. 开发和发布顺序

1. **两版共同改进**：修正热重载提示；ADDINFO 与更多记录类型诊断；完善现有策略表单、变量关联与受限文件管理。无需等待集成镜像即可交付。
2. **集成版基础与首发**：固定核心及依赖；进程管理与配置应用；Unbound/Redis 统计；MosDNS 入口计数和缓存计数；运行状态与日志。首发卖点是“看得清、改得稳”。
3. **集成版功能差异**：可选查询日志与排行、域名黑白名单、数据更新任务，再增加经过验证的多层缓存维护。
4. **设备策略**：先完成缓存隔离，再交付客户端分组、不同上游/过滤策略和 IPv6/HTTPS 记录选项。

验证应覆盖：默认策略与上游解析行为对照；缓存冷热状态；CNAUTO 关闭；失败重载、配置回退与组件崩溃；计数器重置；日志关闭；两个客户端策略交叉查询；订阅失败保留旧数据；容器内存受限；不同 CPU 架构。不要为了增强功能直接替换为另一个普通 MosDNS 二进制，定制插件和模板不是可互换的。

DoH/DoT 服务端、DNSSEC 验证开关、自适应上游评分可以以后单独设计。当前源码并不能证明这些是打开一个开关就能交付的完整功能。
