# 解析可视化、DNS 过滤与放行验收

2026-11-20，源码开发验收。DNS 测试使用无网卡 Debian 12 amd64 虚拟机、独立状态目录、回环端口与合成域名。浏览器测试使用合成记录和临时登录会话。

## A · 解析概览与查询日志

- 固定时间窗口的合计、P95 样本、空时段、旧库、容量淘汰、精确域名和分页游标由 `integrated/tests/test_query_records.py` 覆盖。左闭右开窗口与序列上界一起传到查询日志。只读观察角色拒绝日志和汇总。
- `node scripts/test-query-records.mjs` 通过 1360、390、320 像素测试。点击有流量的时间桶后按来源 IP 缩小范围，URL 保留起止时间与快照。截图写入 `.build/query-records-browser-*/`，其中 `analytics-desktop.png`、`analytics-mobile.png`、`analytics-disabled.png` 分别记录桌面、移动和采集关闭状态。
- `python scripts/benchmark-query-analytics.py` 用合成记录填满 31 MiB 数据库，保留 73,952 条。Windows amd64（16 逻辑处理器、Python 3.14.3、SQLite 3.50.4）本地复测的首次 24 小时汇总为 589 ms，12 次刷新耗时中位数为 0.16 ms。压力期间保存 608 条，队列峰值 176，最终积压和丢弃均为 0，抽样的数据库及事务日志峰值为 32,530,992 字节。初测取消复用时反复计算满库曾出现丢弃。现在同一时间只执行一个重查询，同一窗口短时复用，清空时同步重置汇总。

## B · 规则与真实 DNS 入口

冻结的 `240822.1` MosDNS 二进制 SHA-256 为 `5ad0bcea591cf3a51d45966bf24090ea03258d3e42ed3fef7381f533658ffc38`，Go 元数据记录源码提交 `408e342f64ea0f8bc35aeb7bd456be767e7ed0d3`。基线二进制的 UDP/TCP 都经模板中的 `check_cache`。扩展在调用原入口序列前检查规则，通过 Unix 套接字原子换入活动快照。禁用过滤仍执行原入口序列。两个独立构建的 Linux amd64 扩展文件 SHA-256 均为 `1b287a682161aa0124b94b134e4be473ba844a3da03e88ed303cae3da4e18511`。源码和构建条件由锁文件与产物清单记录。[构建与锁定入口](../packaging/mosdns-filter.lock.json)

- 固定版 AdGuard Home v0.107.76 用同一个回环上游对照 10 个域名，每个域名各走 UDP 和 TCP，判定均符合兼容矩阵。按上游是否收到请求核对 QNAME 匹配，LovestDNS 的本机拦截响应另验为 REFUSED。[对照版本](https://github.com/AdguardTeam/AdGuardHome/releases/tag/v0.107.76) · [官方规则语义](https://adguard-dns.io/kb/general/dns-filtering-syntax/)
- 真实 MosDNS 验证了先预热再拦截。A、AAAA、TXT、HTTPS 查询均可命中，预热域名被拦时上游新增请求数为 0。例外、本域与子域、刷新失败保留旧版本、纯递归的能力状态和重启加载已覆盖。失败回退及中断后以磁盘版本恢复另有自动测试。
- `scripts/test-query-records.mjs` 验证过滤页的当前规则检查、订阅预览及部分导入确认。任务的 DNS 生效由独立的真实核心测试验证。
- 本地 TLS 服务器用 AdAway 快照验证了 HTTPS 握手、证书校验、302 跳转和正文读取。公开源由主机下载留存快照，此环境下应用下载器对该公开源的直接 TLS 连接遭重置。

| 公开订阅快照 | 可用 | 注释或空行 | 无效 | 普通不支持 | 阻止应用 |
| --- | ---: | ---: | ---: | ---: | ---: |
| AdAway hosts | 6541 | 4065 | 0 | 1 | 0 |
| AdGuard DNS Filter | 190859 | 1675 | 103 | 640 | 210 |

AdAway 快照 SHA-256 为 `8c925c37a5eaa5e1c582c76e12301797861addee5ad0fa0e6e46c9dd0136fe55`。AdGuard DNS Filter 快照 SHA-256 为 `7ca216c90994f22c0126ec4c8d64739ea1bb6357b2c7a3078db8ebc0d37880df`。后者含首版无法保留的例外或禁用语义，整份保持待复核。快照仅在本地验收目录使用。[AdAway 源文件](https://github.com/AdAway/adaway.github.io/blob/master/hosts.txt) · [AdGuard DNS Filter 源仓库](https://github.com/AdguardTeam/AdGuardSDNSFilter)

性能样本在同一 Windows 主机的 Debian 12 amd64 QEMU TCG 虚拟机中采集，分配 2 vCPU 和 1536 MiB 内存。每种策略按同一脚本串行查询 1000 次已预热 A 记录，关闭逐请求记录。延迟包含 Python 客户端、回环 UDP 和模拟器调度。指标适用于本轮样本，缺少物理主机负载对照。

| 状态 | P95 ms | P99 ms | 查询/秒 | RSS MiB | 应用 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 冻结二进制 | 4.009 | 4.946 | 422.6 | — | — |
| 扩展，过滤关闭 | 3.973 | 4.753 | 400.1 | 17.0 | — |
| 扩展，小列表 | 2.908 | 3.755 | 545.6 | 18.7 | 58 |
| 扩展，125005 条 | 3.471 | 4.445 | 496.4 | 77.7 | 27680 |

## C · 请求证据与临时放行

- DNS 监听器在同一请求上下文内提交过滤动作、来源、规则快照、版本和实际应答。两个来源在 UDP/TCP 共用同一个 DNS ID，加上同一 TCP 连接复用 ID，共 6 项拦截事件，ID 和来源均能区分。上游单独返回的 REFUSED 保持为继续解析的判定。
- 用户点击「精确放行 10 分钟」会写入 600 秒到期值。真实链路核对本域恢复、子域仍拦，并用 2 秒绝对到期值加速验证自动失效；探测使用原入口，无需重启核心。旧记录保留原版本。关闭记录或清空后的延迟事件不入库。
- 浏览器合成链路覆盖「图表 → 时间窗口 → 来源 IP → 命中规则 → 精确放行」，保存为 `analytics-block-and-allow.png`。逐请求缓存与分流路径属于阶段 D。

## 部署验收

11 月 20 日后续改版的本地验收覆盖一级广告过滤导航、从首页打开当时的来源历史和拦截详情、订阅与放行页签。秋风公开订阅本次快照导入 982 条有效规则，3 条重复，普通不支持、无效及阻止导入均为 0。新增的 4 条两端通配规则用固定版官方 AdGuard Home 和带扩展的 MosDNS 在 Debian 12 隔离环境分别通过 UDP、TCP 对照。用例包含相似域名、尾部额外字符和高优先级放行。已有订阅在刷新复核前维持原语义。

- Debian 12 amd64 实例通过 systemd 启动，使用发布版 0.1.1 和保留原组件的本地候选包 `0.1.1-filter-qa` 演练。安装器校验候选包摘要和全部载荷。发布版的实例 ID 和被动查询记录在升级后保留，候选版启用 MosDNS 事件采集。真实 UDP/TCP 命中记录含活动版本，各传输均通过。精确放行仅恢复目标域名，重启后活动规则和历史证据可用，清空记录后过滤仍生效。回退到发布版后原 DNS 应答恢复。隔离机 `/etc/resolv.conf` 摘要在验收期间保持一致。
- 同一候选包以 `CNAUTO=no` 单独安装。UDP 和 TCP 都由 Unbound 直接返回预期地址。两个查询均写入日志，来源标记为入口报文采集，过滤状态报告为纯递归模式下不可用。
- Docker 候选层的扩展文件、摘要及构建元数据安装检查通过。当前 Windows 主机缺少可用的 Docker 引擎和 WSL 服务，完整镜像构建和容器实测留待集成 CI。CI 的 `--require-filter` 路径会检查预热过滤、UDP/TCP 命中证据、临时放行和恢复拦截。
- Debian 13 未运行安装演练。原生验收包复用了 0.1.1 发布载荷中的冻结组件和 wheel，源码发布构建仍由原生打包工作流执行。

## 回归结果

| 项目 | 结果 |
| --- | --- |
| Debian 12 的集成后端与脚本 | 737 项通过 |
| Debian 12 的独立版后端 | 46 项通过 |
| 集成和独立版前端单测 | 57 项通过 |
| API 契约 | 37 个固定样本回放和校验通过 |
| 集成 UI 构建和独立版检查点 | 通过 |
| 浏览器交互 | 1360、390、320 像素通过 |

## 可重跑命令

```sh
python -m pytest integrated/tests scripts/tests -q
python -m pytest web/backend/tests -q
python scripts/export-api-contract.py --check
node --experimental-strip-types --test integrated/frontend/tests/*.test.ts
node --experimental-strip-types --test web/tests/*.test.ts
python scripts/build-integrated-ui.py --reuse-dependencies
node scripts/test-query-records.mjs
python scripts/benchmark-query-analytics.py
python scripts/test-dns-filter.py --mosdns /path/to/mosdns-filter \
  --baseline /path/to/frozen-mosdns --adguard-home /path/to/AdGuardHome \
  --list /path/to/snapshot.txt --queries 1000
```

新构建的 Docker 候选用 `scripts/test-integrated-container.py --require-filter` 验证。带测试标记的原生隔离实例可用 `scripts/test-native-instance.py --require-filter` 验证。
