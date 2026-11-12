# 独立 DNS 组件构建

组件来源由 `packaging/components.lock.json` 固定。构建工具为 Docker；原生部署运行时仍不依赖 Docker。

当前按用户确认的[十台以内终端家用范围](../../docs/home-use-acceptance-2026-11-11.md)推进：优先核对解析、分流、缓存恢复、持续运行与资源占用，暂停微秒级性能排名。下文保留独立组件及原性能工具的使用说明，原性能失败和未完成范围不改判为通过。

[30 分钟受控验收](../../docs/home-use-results-2026-11-11.md)已完成，18,854 条查询全部正确；慢应答、资源增长、试验范围与在用状态保护均单列记录，不代表默认升级或真实局域网验收完成。

```sh
python3 scripts/build-components.py --output .build/components-bundle
python3 scripts/extract-component-baseline.py --output .build/core-baseline
python3 scripts/test-cache-compatibility.py \
  --baseline-home .build/core-baseline --bundle .build/components-bundle \
  --work .build/cache-compatibility
python3 scripts/test-dnscrypt-compatibility.py \
  --baseline-home .build/core-baseline --bundle .build/components-bundle \
  --work .build/dnscrypt-compatibility
```

输出目录必须是新目录，已存在的源码下载会重新核对 SHA-256。构建固定 amd64、基础镜像 digest 和主要构建/运行依赖版本，输出实际包清单与逐文件摘要。Redis 保持 libc 分配器；Unbound 的 OpenSSL、libevent、hiredis 和 musl 随组件隔离打包；DNSCrypt 使用校验过的官方发行包。定制 MosDNS 与原运行库保留。

原生包在现有构建命令中增加 `--components-bundle .build/components-bundle`。`--components redis` 等参数用于单组件对照；省略 bundle 仅生成原上游基线。集成容器默认 candidate 目标读取同一目录，`--build-arg DNS_COMPONENTS=redis` 可选择组件；`--target upstream` 构建原始组件基线。

修改组件、依赖或构建参数后重新构建，并执行实际缓存读写与运行回归。锁中的文件格式一致不等于应用缓存语义兼容。此清单首轮仅验证 amd64，其他架构需要独立清单、摘要和运行证据。

Unbound 旧大小写键的离线准备、冲突和 CNAME 中间项处理见 [离线缓存兼容说明](../../docs/cachedb-offline-upgrade.md)。新增真实签名、负应答、Redis 持久化和回退工具；本轮实际结果与仍保留的性能关卡见 [组件收尾记录](../../docs/component-closure-results-2026-11-10.md)。

DNSCrypt combined 的推荐决定及实际历史缓存回放、容量、Unbound 性能和匹配原生组合包的后续验收见 [Unbound 收尾记录](../../docs/unbound-completion-results-2026-11-10.md)。各项结果独立保留；候选包中的明确版本不表示已修改默认组件或获得性能批准。

[性能与容量边界补测](../../docs/component-boundary-followup-results-2026-11-10.md)保留修正发生器 CPU 后仍未通过的 Unbound 校准，以及 Redis 淘汰强度 30 的容量收益和写入延迟代价；该设置尚未进入默认模板或既有组合包。

首轮升级保留 DNSCrypt 原有协议策略：新版默认新增的 `pqdnscrypt` 在生成的运行配置中显式关闭，未指定的 `lb_strategy` 显式使用旧版 `p2`；用户在模板中已设置这些项时保留用户选择。DoH、HTTP/3、缓存及解析器列表继续沿用现有配置。协议扩展需另行验证后开启。

性能工具 `scripts/benchmark-dns.py` 使用 dnsperf 的逐请求记录计算 P50/P95/P99，保存原始记录的 gzip 文件、过程资源采样和输入摘要。默认 120 秒、5 次重复，交错版本顺序。必须在构建和其他压测结束后运行；短时 smoke 不能代替默认参数的性能验收。测试路径使用独立缓存与本地受控上游，不修改运行实例的数据。

Linux 测量机需准备支持 `-v` 逐请求延迟的 dnsperf（本轮使用 2.14.0）。保存实际工具摘要及依赖版本；给测试进程固定相同 CPU 范围，并检查与网关等共存进程的负载。

Unbound 入口可选 `--cpu-layout controller=0,fixture=1,redis=2,unbound=3,capture=4,generator=5+6`，须在允许这些 CPU 的 Linux 隔离环境内使用。此模式把上游放入独立 Python 进程，各角色 CPU 集合互不重叠，Redis 准备和组件重启沿用同一分配，报告核对实际 affinity。dnsperf 有独立收发线程，[官方建议每个客户端提供两个 CPU](https://www.dns-oarc.net/tools/dnsperf)；`generator=5+6` 同时核对各线程的允许 CPU 集合。固定 affinity 不表示独占宿主 CPU，也不保证消除虚拟机停顿。校准、原门槛及保留失败规则见[补充验证方案](../../docs/component-boundary-followup-plan-2026-11-10.md)。

答案验证使用独立进程旁路抓取本轮 loopback UDP 包，核对输入问题与每条 A/AAAA 答案，不增加转发跳数或额外 DNS 查询。运行压测需要 Linux `CAP_NET_RAW`（以下示例使用 root）；没有权限时直接失败。抓包丢包、抓包数量与 dnsperf 数量不一致、错误/空答案，均不能通过验收。抓包进程 CPU/RSS 单列，避免与受控上游争用同一个 Python 解释器。

```sh
sudo python3 scripts/benchmark-dns.py \
  --baseline-home .build/core-baseline --bundle .build/components-bundle \
  --dnsperf /usr/bin/dnsperf --work .build/performance \
  --duration 120 --repeats 5 --qps 1000 \
  --cases baseline,redis-only,unbound-only,combined,combined-20ms \
  --scenarios hot,mixed90
python3 scripts/report-dns-benchmark.py .build/performance/results.json \
  --output .build/performance/report.json
python3 scripts/test-cachedb-timeout.py \
  --baseline-home .build/core-baseline --bundle .build/components-bundle \
  --work .build/redis-stall
```

`--entry mosdns` 通过冻结 MosDNS 的独立缓存/转发入口测量，默认直接访问 Unbound；前者用于入口热缓存开销对照，不替代真实分流规则回归。`combined-20ms` 仅改变候选 Unbound 的 Redis 命令超时，不修改默认组件配置。报告中的 CPU 和 RSS 汇总包含所选入口的 DNS 进程，负载工具与受控上游另列；固定提供 QPS 不等于最大吞吐。

汇总同时检查每轮 dnsperf 实际运行时间、计数一致性、逐请求延迟样本、答案校验与资源样本。发送量允许目标的 ±1%，有效 QPS 至少达到目标的 98%（包含末尾应答等待）；每轮都要满足，不能用中位数掩盖不足负载。旧结果缺少实际运行或逐条答案校验记录时不予通过。客户端 UDP 收包队列与 socket 丢包计数随过程采样保存，可辅助定位负载工具的收包问题。

SOCKS 兼容测试逐组记录新的连接数与 DoH 请求路径。测试代理使用独立 loopback 源地址，SOCKS 用例中的 DoH 服务拒绝直连；上一组的代理连接不能作为下一组的通过依据。

Redis 命中场景使用 `--scenarios cachedb`。准备阶段的 `unique-question-coverage-v2` 协议保存逐问题账本，核对唯一问题、答案和缓存键；仅允许有账本证明、且不超过唯一问题数 1% 的额外上游尝试。正式测量仍要求每条查询恰好一次 Redis 命中、零未命中、零上游请求。历史结果继续按原严格计数解释，详见 [后续验收方案](../../docs/component-maintenance-next-validation-2026-11-09.md)。

加密转发另用 `scripts/benchmark-dnscrypt.py`，直接比较旧版与仅更新 DNSCrypt 两种组合。在已启用 loopback 的独立网络命名空间中执行：

```sh
python3 scripts/benchmark-dnscrypt.py \
  --baseline-home /path/to/core-baseline --bundle /path/to/components-bundle \
  --dnsperf /path/to/dnsperf --work /path/to/new-dnscrypt-comparison \
  --duration 120 --repeats 5 --qps 100
python3 scripts/report-dns-benchmark.py /path/to/new-dnscrypt-comparison/results.json \
  --output /path/to/new-dnscrypt-comparison/report.json
```

两条路径分别为真实 TLS DoH 直连和经 SOCKS 转发；临时证书只提供给测试子进程，保留 TLS 验证。每轮使用唯一 A/AAAA 冷查询，检查实际上游工作量及正确路由，不把缓存命中算作加密转发性能。HTTP/1.1 夹具不覆盖 HTTP/2、HTTP/3 或 DNSCrypt 协议，也不代表公网解析器或真实分流规则。CPU/RSS 只汇总 DNSCrypt，夹具、代理、发生器及抓包资源另列。
