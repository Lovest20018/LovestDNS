# LovestDNS API v1 开发候选基线

候选：`0.1.0-dev.lovestdns-native.20261104.11`，API 主版本：`1`。这是可追踪的开发基线，**不是稳定发行**。LovestDNS 可独立运行，DNS 管理 作为可选消费者接入。历史容器验收见 [10-26 记录](../../verification.md)，Debian 原生候选范围另见 [11-04 测试机记录](../../verification.md)。能力接口提供 `deployment=native/container`，DNS 默认地址与端口由当前部署参数发现；配置条件保存与应用记录由独立能力字段发现。未改动 88 的生产服务、推送镜像或创建稳定标签。

## 文件与身份

- `openapi.json`：真实 `/api/v1/openapi.json` 处理函数导出的 OpenAPI 3.1 文档，直接返回文档，不包 `data/meta`。
- `samples.json`：22 组请求/响应，保存路径、HTTP 方法/状态/追踪头、角色与场景来源，不含凭据。
- `manifest.json`：API/候选版本、源码文件 SHA-256 清单及汇总、契约/样例摘要及历史运行验收的提交与范围。

在本仓库根目录执行以下命令取得包含该基线的本地提交：

```sh
git log -1 --format=%H -- docs/contracts/v1/manifest.json
```

提交自身的哈希不能写入同一次提交的文件而仍保持相同哈希，因此清单用确定性源码摘要绑定内容，用上述 Git 命令定位提交。`source.files` 覆盖集成代码/测试、冻结 WebUI、打包与脚本及列明的根配置；不涵盖所有文档，也不是整棵 Git tree 的替代标识。`source.sha256` 是该路径到摘要映射的规范 JSON 摘要。文本按 LF 计算，以便 Windows/Linux 核对；契约 JSON 使用 UTF-8、键排序、两空格缩进、末尾 LF。

`instance.version` 和 `capabilities.version` 是此 DNS 候选版本。`capabilities.core.source_commit` 是锁定的**上游 DNS 核心**提交，不是本仓库候选提交。API 主版本与软件候选版本分别记录。

## 样例来源与确定性

所有响应均由真实 Flask 测试客户端经过路由、Bearer 鉴权、权限边界、处理函数、封装与追踪头产生。没有手写响应，也没有从真实家庭环境抓取数据。每例的 `provenance.response=real_flask_handler` 与 `scenario=synthetic_test` 同时成立：**真实处理逻辑，人工测试输入**。

导出仅替换 I/O/时钟边界：临时状态目录与公开的固定实例 UUID、固定 Unix UTC 时间 `1792972800`（2026-10-26 00:00:00 UTC）、显式 `contract-...` request ID、合成采集器/维护服务输入、锁文件内公开核心元信息、DNS wire 回复。DNS 成功/NXDOMAIN 经真实查询构造、wire 解析和结果判断；指标零值和不可比较窗口经过真实 `observer.rate` 与指标处理函数。固定单调时钟使 DNS `elapsed_ms=0`，它是测试值，不是性能测量。导出不会连接网络、内部 socket、真实配置或真实核心，不代表现场验收。

样例覆盖：observer 实例/能力（采集正常及不可用）、正常/部分组件异常/过期/不可用 runtime、分层 metrics 的 QPS 真零值与空命中率、重启/首次采样不可比较、组件不可用、metrics 的 503、无凭据 401、observer 配置读取 403、管理员条件创建文件、版本与 ETag 读取、过期条件写入 412、配置记录待确认、DNS 成功、NXDOMAIN、混合结果的 UDP/TCP 两链路对照、样本健康检查。每组响应头与 `meta.request_id` 一致，错误没有 `data`。

清单不写导出当前时间、随机实例 ID、当前 HEAD 或真实路径；同一源码重复导出逐字一致。源码摘要变化表示提供方实现或校验代码变化；单独比较 `artifacts` 可识别实际契约/响应是否变化，避免把源码改动误当成 API 字段变化。所有重新生成的差异仍需代码审阅，不应在 DNS 管理 CI 中自动更新期望值。

## DNS 管理 可重复执行的命令

使用有 Flask、PyYAML、pytest 和 jsonschema 的 Python 3.12+ 虚拟环境。从 DNS 仓库根目录运行，后端两套测试必须分进程（它们分别导入独立版和集成覆盖层）：

```sh
python -m pip install -r web/backend/requirements.txt -r integrated/tests/requirements.txt
python scripts/export-api-contract.py --check
python -m pytest integrated/tests scripts/tests -q -p no:cacheprovider
python -m pytest web/backend/tests -q -p no:cacheprovider
python scripts/freeze-standalone.py
node --experimental-strip-types --test integrated/frontend/tests/*.test.ts web/tests/*.test.ts
```

`--check` 重新调用真实提供方并按 OpenAPI 校验请求、响应、头及 request ID，再核对已提交文件和源码摘要；漂移时退出非零且不覆盖文件。DNS 管理 可固定上述 Git 提交和 `manifest.json`，直接消费 `samples.json.cases`，按 `request.role` 选择测试身份；它不是凭据值。

有意调整提供方后，审阅差异并重新导出：

```sh
python scripts/export-api-contract.py --write
python scripts/export-api-contract.py --check
# 可导出到临时目录供比较，不覆盖仓库内快照：
python scripts/export-api-contract.py --write --output .build/DNS 管理-contract-review
```

本开发机的 jsonschema 已位于被忽略的 `.build/test-deps`，本次未安装依赖；PowerShell 可先执行 `$env:PYTHONPATH = (Join-Path $PWD '.build/test-deps')`。这是本机现有依赖位置，不是 DNS 管理 必须采用的配置。

## 必须保持的兼容语义

1. 首期 DNS 管理 使用 `SUITE_READ_TOKEN`，先接 instance/capabilities/runtime/metrics，再接 dns-test/dns-compare/health-check。配置、日志、历史和维护不在 observer 权限内，配置操作深链到 DNS 面板 `#/config` 等入口。`/api/configs` 仍为 404；observer 访问 `/api/v1/file/...` 是预期 403。不要在鉴权或采集失败时自动回退旧接口。
2. 采集目标周期 5 秒，新鲜度上限 25 秒，恰好 25 秒有效；超过 25 秒、未来或无效采样时间返回 HTTP 503 / `STALE_DATA`。采集不可连接返回 503 / `UNAVAILABLE`。DNS 管理 可保留最后证据，但必须显示过期/失败。
3. `sampled_at` 等数值时间为 Unix UTC **秒**，可含小数；`checked_at`/`started_utc` 是带时区文本。快照新鲜不等于核心健康，HTTP 200 也不证明 DNS 解析或客户端业务正常。
4. 指标保留 source/layer/unit/window_seconds/available/unavailable_reason；真实无查询的 QPS 可以为 0，命中率无分母为 null；首次采样、计数重置、进程变化或组件不可用用 null。两路 Unbound 不相加伪造 MosDNS 入口总量。
5. `X-Request-ID` 合法格式是 8–64 个 ASCII 字母、数字、下划线或短横线，合法值透传，不合法值生成新值；响应头和业务 `meta` 一致，包括错误。它与维护任务正文的 32 位小写十六进制幂等编号不同。
6. `permissions` 是访问边界；`features` 还取决于实际可用能力，不能只按版本/镜像推断。observer 的 config_history/config_apply/redis_snapshot 均为 false。
7. 实例 ID 存在状态卷的 `instance.sqlite`，Web/worker/容器同卷重启保持身份，删除状态卷产生新身份，克隆状态卷复制身份。ID 不是 boot ID；组件重启靠比较窗口和 `comparison_reason` 处理。不能因 ID 未变继续使用旧指标窗口；DNS 管理 对换实例/重复克隆身份应确认关联。
8. `dns_defaults.server=127.0.0.1` 指 DNS 集成容器自身；DNS 管理 主机可访问的 DNS 地址是另一个设置。DNS API 探测来自 DNS 服务端，不能表述成选定终端的探测。DNS HTTP 200 / NXDOMAIN 的 `available=false`、`status=dns_error`、`rcode=NXDOMAIN` 均须保留。健康检查只验证内置样本，链路对照最多四条。
9. 维护 `expected_sha256` 保护预览后应用的输入；**不等于文件保存并发版本检查**。当前 `{content}` 保存没有 expected_revision/If-Match 条件保存，本轮不扩 scope。保存、应用、解析验证仍是不同阶段。
10. 既有 v1 字段类型/含义保持；消费者忽略未知附加字段，破坏性变更需要新主版本。样例只覆盖首期联动情景，不枚举所有管理 API；其余管理路径继续由现有提供方契约测试覆盖。

## 2026-10-26 本地结果与剩余验收

- 集成/脚本：127 passed（新增 19 项提供方基线检查及 3 项冻结可移植性检查；此前为 105 项）。
- 独立版后端：46 passed；两版前端：14 passed。
- 集成版 TypeScript/Vite 构建、ESLint：通过；独立版源码冻结：39 文件和原 ZIP SHA-256 通过。
- 两次导出一致性、对本机凭据/路径隔离、契约漂移非零退出、实例新进程/并发/克隆身份及新鲜度边界已测试。
- Windows 开发机仍无 Docker CLI；后续通过 Xterminal 在 隔离测试环境 完成了容器构建及隔离运行验收，包括核心监督、CNAUTO 双模式、配置应用/失败回退、RDB 保留、Web 重启和卷身份。明细及剩余范围见 [验收报告](../../verification.md)。未重跑浏览器 UI，DNS 管理 端联调由 DNS 管理 聊天负责。

独立检查点 `standalone-20261024` 的 39 文件、冻结清单和原始 ZIP 保留不变。忽略规则排除凭据、运行数据库、缓存、截图和构建目录；唯一纳入版本控制的 ZIP 是已审阅的独立源码检查点。

原冻结清单的四个文本文件仍按原始 CRLF 字节记录摘要；为让 Linux/LF 检出也能验证，冻结脚本先校验原 ZIP 的精确 SHA-256，再仅允许当前源码与 ZIP 的 CRLF/LF 表示差异。源码内容变化或 ZIP 字节变化仍拒绝；没有重建或改写检查点。
