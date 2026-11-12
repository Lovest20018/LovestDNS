# DNSCrypt 普通制品的受控分流链路验证

从 `f9b69ea` 继续。旧部署 DNSCrypt、同工具链 2.1.20 原实现和独立缓存分片补丁作为三种候选；三者共用相同的旧 Unbound、Redis 和冻结的定制 MosDNS。只在 41 新目录与网络命名空间运行，不操作在用配置、生产缓存或 旧版正式机，不提 PR。

## 实际链路与可解释场景

上游的 MosDNS 是缓存和分流入口；本地 Unbound/Redis 与 DNSCrypt 是不同分支，实际规则还可能并发或回退。夹具以受控 local.cache.test 域名走旧 Unbound/Redis，其余 enc.cache.test 走 DNSCrypt → HTTP/2 DoH（直连或实际 SOCKS）。冻结 MosDNS 的二进制保持原样，配置只保留解释该分支所需的域名匹配、缓存和转发；不把它称为生产地理规则、并发上游或故障回退的完整覆盖。

四个负载：

- DNSCrypt 直达冷查询，每个问题唯一，全部应访问 DoH。
- DNSCrypt 直达热点，预热后全部由其缓存应答，不应再访问 DoH。
- MosDNS 入口冷查询，local/enc 各半；逐一核对本地上游的问题名/类型/类和 DoH 请求数。
- MosDNS 入口热点，预热后不应访问本地上游、DoH 或 Redis；用它验证上层缓存遮挡，不能期待所有请求都享有 DNSCrypt 内部优化。

固定 100 QPS、q64、20% AAAA，600 秒 TTL。三种候选记录实际二进制/私有库摘要；本地缓存全部新建，Redis 无持久化。每个计量组都汇总 DNSCrypt/MosDNS/Unbound/Redis 的进程 CPU 和采样峰值 RSS，并保留各组件值；RSS 峰值之和不是同一时刻实测整体峰值，空闲组件的零 CPU 也不作百分比收益。

## 有限执行与关卡

1. 检查工具反例：错分流、隐藏回源、错类型、数量或 ALPN 必须拒绝。新方法与输入均独立记录，既有无效轮次不追认。
2. 三候选 × 四负载 × 直连/SOCKS，每组 5 秒一轮，共 24 组路径短测。先执行分流冷/热，再执行 DNSCrypt 直达冷/热。
3. 只有前轮全部测量条件有效、临时任务没有计量限流，才做直连下两个冷查询负载的 30 秒、三轮初筛，共 18 组。先检查源码补丁相对原实现和旧部署的 CPU/RSS/延迟差异；CPU +5%、峰值 RSS +10%、P95/P99 允许 `max(基线×5%,0.2ms)` 的既定筛选门槛保持。初筛不批准版本升级。
4. 120 秒五轮、全部路径验收仍是后续独立关卡。此前 41 曾出现逐包发送空档，本阶段因新增普通制品、路径和输入建立新的有限基线；若再次出现则保存并停止本轮性能签署，不在相同条件下重复直至通过。

沿用内核逐包条件：相邻请求 5–50 ms、每个 1 ms 桶最多一包、查询/答案/内核往返配对完整、答案与协议正确、socket/抓包零丢包。平均 QPS 合格不掩盖发送空档。任一测量条件失败即停止该轮，保留输出；未跑的组合不能标为通过。DNSCrypt 子进程去 NET_ADMIN，Go 组件 GOMAXPROCS=2；临时任务 CPU 4–7、400% 配额、1 GiB 内存、低优先级，逐步记录资源计数并监测在用服务健康。

组件默认锁、源码补丁、Redis/Unbound 既有判定、MosDNS 冻结均保持。完整发布组合、UDP 补丁权限/内核参数、升级回退和持续观察仍不由本阶段替代。

## 复现入口

先按 `packaging/dnscrypt-source.lock.json` 构建 upstream/cache 普通候选与 Go DoH、定速 UDP 夹具，将清单与制品放入同一候选目录。入口校验源锁、补丁、二进制和夹具源码摘要，拒绝诊断制品。旧部署目录只作为只读二进制来源；不要把测试 work 指向运行目录。

```sh
unshare --net sh -c 'ip link set lo up && exec python3 scripts/benchmark-dnscrypt-chain.py \
  --baseline-home /opt/lovestdns-validation/current \
  --candidates /inputs/normal --work /tmp/chain-smoke \
  --duration 5 --repeats 1 \
  --scenarios split-cold,split-hot,dnscrypt-cold,dnscrypt-hot'
python3 scripts/report-dnscrypt-chain.py /tmp/chain-smoke/results.json \
  --output /tmp/chain-smoke-summary.json
```

三轮初筛另用新 work，指定 `--duration 30 --repeats 3 --routes direct --scenarios split-cold,dnscrypt-cold`。计量时同时运行服务健康及 cgroup 限流/OOM 监测，报告工具不能替代外部资源守护。P95/P99 来自定速客户端的请求往返时间，另存内核时间戳往返数据；两者都不等于组件纯处理时间。失败后留下原目录，避免覆盖。

报告按每百万次完成请求归一化 CPU，再取各轮中位数，分别检查 DNSCrypt 与四组件总和；不能用其他组件的下降抵消 DNSCrypt 回退。缺组、重复轮次、无效测量或缺少必要检查项不能通过。5 秒短测无论差异如何都不能算性能初筛通过；30 秒三轮也不能算 120 秒五轮通过。工具始终输出 `release_approved=false`。
