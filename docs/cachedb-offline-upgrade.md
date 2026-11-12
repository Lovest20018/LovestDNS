# Unbound 离线缓存兼容准备

`scripts/prepare-cachedb-upgrade.py` 在 Linux / Python 3.11+ 上处理**已经完成的 RDB 副本**，需要按组件锁验证的候选 bundle。它只连接自己新启动的 Redis Unix socket，禁用 TCP、AOF、自动快照和淘汰；工作目录必须不存在。该工具不接收在用 Redis 地址，不执行安装或重启。

## 保留规则

- DB 0、1 各自提供准确的 `secret-seed`，按 DNS 标签字节、问题类型和类核对旧 SHA-256 键，再派生小写查找键。种子或格式无法核对时跳过。
- 只增加缺失的小写目标，保留全部原键和已有目标。多个旧大小写变体只有在问题大小写以外的整段内容、写入／到期时间以及 Redis 绝对截止时刻完全相同才可合并；冲突不选取任意历史地址。
- 不修改 DNS 报文、TTL、签名、cachedb 两个时间戳或 Redis 绝对过期时刻。DNS TTL 已过期不会因此变新；实际应答继续由 Unbound 的过期和验证配置决定。
- CNAME 中间项缺少所查类型的最终记录时，整个小写目标分组跳过，交给自然刷新。未知报文、其他 Redis 类型、种子不匹配和各类记录单独计数。该规则保守处理已识别的中间项，不等同于验证任意历史报文的完整解析链。
- 默认最多扫描 300 万键、检查 2 万个旧大小写键、64 MiB 候选值数据；超过预算退出。执行时重新派生计划，完整核对所有源与目标，再用原子命令按原字节、原截止时刻增加键。自然 Redis 过期可跳过，其他变化拒绝执行。

## 两步操作

准备私有 `seeds.json`，明确包含字符串字段 `"0"` 和 `"1"`。值必须与输入快照所属两路 Unbound 的种子一致；不要将文件提交或上传到公共报告。

先生成计划：

```sh
python3 scripts/prepare-cachedb-upgrade.py \
  --input-rdb /private/snapshot-original.rdb \
  --bundle /private/verified-components-bundle \
  --seeds-file /private/seeds.json \
  --work /private/new-plan
sha256sum /private/new-plan/plan.json
```

查看 `result.json` 的分类、冲突和预算，以及 `plan.json` 的源／目标指纹。计划不包含明文域名或种子，但仍应作为私有缓存材料保存。确认计划后，以实际文件摘要执行到另一个新目录：

```sh
python3 scripts/prepare-cachedb-upgrade.py \
  --input-rdb /private/snapshot-original.rdb \
  --bundle /private/verified-components-bundle \
  --seeds-file /private/seeds.json \
  --work /private/new-prepared \
  --apply-plan /private/new-plan/plan.json \
  --expected-plan-sha256 '<reviewed plan file SHA-256>'
```

输出为 `new-prepared/cache.rdb`，另有结果、计划和独立 Redis 日志。输入摘要在复制和退出时复核；回退依据仍是原快照。准备阶段在 Redis 加载完成、即将写新快照时释放本工具的载入副本，降低同时保留大文件的数量；若保存失败，原始输入仍保留。不要直接把输出复制到运行数据目录：还需隔离加载、实际 DNS 查询、容量／淘汰及安装器回退验证。

## 验证入口

```sh
python3 scripts/test-cachedb-case-upgrade.py \
  --baseline-home /path/to/deployed-baseline \
  --bundle /path/to/verified-components-bundle --work /private/new-case-test
python3 scripts/test-redis-persistence-upgrade.py \
  --baseline-home /path/to/deployed-baseline \
  --bundle /path/to/verified-components-bundle --work /private/new-persistence-test
```

前者用真实 RSA DNSSEC 签名、错误签名、签名过期、CNAME 中间项、A/AAAA/TXT、负应答及两数据库验证新旧查询和回退。签名过期夹具对两版都显式设定零时钟容差，避免默认容差将短期过期继续视为有效；正式配置不变。相关语义见 [Unbound 官方配置文档](https://unbound.docs.nlnetlabs.nl/en/latest/manpages/unbound.conf.html#term-val-sig-skew-min)。后者以受控二进制值和六种 Redis 类型验证新版后台快照、重载、旧版读取以及原快照恢复。

这两个工具只给出各自范围的兼容结果，不签署真实全量历史缓存回放、容量压力、AOF、崩溃恢复或 DNS 性能通过。本轮结果见 [组件收尾记录](component-closure-results-2026-11-10.md)。

另有 `scripts/test-cachedb-history-replay.py` 和 [Go 回放辅助工具](../scripts/fixtures/history-replay/README.md)，用于在仅有 loopback 的独立网络命名空间中，从私有准备副本导出完整可核对的问题集，对比新旧普通 Unbound 的真实应答。两数据库、原字节及原时间保持不变；删除标记、无法核对的键、缺失目标和完整 DNS 解码失败分别计数。普通应答比较所有 RR 内容与重数、负应答 authority 和 AD 标志，明确忽略 TTL 及 DNS 名称展示大小写；原始零寿命或 CNAME 中间项的特定刷新差异单列。已识别的解码失败输入要求新旧原始应答逐字节一致，不据此认定报文合法。历史 TTL 已过期不会被改成新鲜数据，该入口也不提供性能或部署批准。
