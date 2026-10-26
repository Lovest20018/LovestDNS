# PaoPaoDNS 集成 API

本项目定位为 DNS 专用管理面板和 API 服务。集成版提供 `/api/v1/` 供独立家庭运维平台调用；ROS、设备清单、跨设备故障关联、告警和长期趋势由该平台负责。DNS 面板保留详细配置与故障恢复入口，可在外部平台故障时直接使用。

浏览器界面和外部运维平台通过同一套后端校验、配置历史和维护任务机制；旧版 `/api/...` 路径保留兼容。v1 的已有字段语义保持兼容，可增补字段；调用者应忽略未知字段。改变字段类型或含义时使用新的主 API 版本。当前为开发候选，容器运行验收完成前不标记稳定发行。

## 认证与角色

- `Authorization: Bearer <WEB_UI_TOKEN>` 是管理 Token，可以读取、诊断、修改配置和提交受控维护任务。
- `Authorization: Bearer <SUITE_READ_TOKEN>` 是只读 Token，只能读取实例、能力、运行快照、指标和执行 DNS 查询/健康检查。不要把管理 Token 放入浏览器以外的长期自动化任务。
- `SUITE_READ_TOKEN` 未设置时不开放外部只读角色。它应与管理 Token 使用不同值。Compose 已支持 `SUITE_READ_TOKEN` 和实例名称 `SUITE_INSTANCE_NAME`。
- API 不向 RouterOS 或其他设备转发任意命令，也没有通用 shell、Docker 或任意路径接口。

## 通用约定

除直接返回 OpenAPI 文档的 `/openapi.json` 外，每个响应都包含 `meta.api_version=1` 和 `meta.request_id`。客户端可以提交 `X-Request-ID`（8–64 个字母、数字、下划线或短横线）；不符合格式或未提交时由服务端生成。响应也会返回同名 `X-Request-ID`，适合把外部平台告警与服务端日志关联。服务端访问日志记录角色、路由模板、状态码和耗时，不记录 Token、请求正文或查询参数。

成功响应格式如下：

```json
{
  "data": {},
  "meta": {"api_version": "1", "request_id": "external-check-123"}
}
```

错误响应格式如下：

```json
{
  "error": {"code": "INVALID_ARGUMENT", "message": "..."},
  "meta": {"api_version": "1", "request_id": "..."}
}
```

HTTP 200 表示请求本身完成，不等于 DNS 解析成功。DNS 查询应同时检查 `data.available`、`data.status`、`data.rcode` 和目标记录。监控数据带有来源、单位、采样时间；不可比较或采集失败的值使用 `null`，不补成 0。

## 主要接口

| 方法 | 路径 | 只读 Token | 用途 |
|---|---|---:|---|
| GET | `/api/v1/instance` | 是 | 持久实例 ID、版本和发行版类型 |
| GET | `/api/v1/capabilities` | 是 | 版本、核心来源、权限、记录类型和当前能力 |
| GET | `/api/v1/runtime` | 是 | 组件状态、两路 Unbound、Redis 和持久化快照 |
| GET | `/api/v1/metrics` | 是 | 带来源和单位的 QPS、命中率、Redis 内存指标 |
| POST | `/api/v1/dns-test` | 是 | 查询单个域名和记录类型 |
| POST | `/api/v1/dns-compare` | 是 | 并行对照最多四条明确 DNS 链路 |
| GET | `/api/v1/health-check` | 是 | 快速检查两个样本域名 |
| GET/PUT | `/api/v1/file/{filename}` | 否 | 白名单配置文件的读取和保存 |
| GET | `/api/v1/history/{filename}` | 否 | 查询最多 20 个历史版本 |
| GET | `/api/v1/history/{filename}/{revision}` | 否 | 查看指定历史版本 |
| POST | `/api/v1/history/{filename}/{revision}/restore` | 否 | 校验并恢复单文件版本 |
| GET | `/api/v1/config/apply-plan` | 否 | 生成配置预览和输入指纹 |
| GET/POST | `/api/v1/maintenance/tasks` | 否 | 查询或提交幂等维护任务 |
| GET | `/api/v1/maintenance/tasks/{task_id}` | 否 | 按编号查询任务状态与结果 |
| GET | `/api/v1/core-logs/{component}` | 否 | 读取最近 200 行脱敏日志 |
| GET | `/api/v1/openapi.json` | 是 | 获取机器可读接口契约 |

完整参数、响应和错误码以运行实例的 `/api/v1/openapi.json` 为准。OpenAPI 3.1 为每个业务接口定义具体返回结构，并通过真实处理函数响应的 JSON Schema 校验。

供 dns 固定使用的开发候选 OpenAPI、18 组确定性脱敏样例及源码/契约摘要见 [v1 提供方基线](contracts/v1/README.md)。从仓库根目录运行 `python scripts/export-api-contract.py --check` 可重放真实处理函数并检查漂移，`--write` 用于审阅后重新导出。当前候选为 `0.1.0-dev.dns.20261026.1`；不是稳定发行，容器运行验收仍未完成。

## 时间、指标和失败语义

- `sampled_at`、`created`、`updated`、`saved_at` 等数值时间均为 Unix UTC 秒，可含小数。`checked_at`、`started_utc` 为带时区的 ISO 8601 文本。
- `interval_seconds` 是采集器目标周期（当前 5 秒）；`window_seconds` 是计算该次增量的实际间隔。QPS 为 queries/second，命中率为 0–1，Redis 内存为 bytes。两路 Unbound 不能相加当作客户端总流量。
- `freshness.age_seconds` 表示样本年龄，`max_age_seconds=25`。过期或时钟不一致返回 HTTP 503 / `STALE_DATA`；采集器不可连接返回 503 / `UNAVAILABLE`，不会用上次快照伪装当前正常状态。
- 本层 `comparison_reason` 区分 `first_sample`、`process_changed`、`counters_reset`、`missing_counters`、`clock_changed` 与 `comparable`。不可比较时 QPS 和命中率为 null；本窗口无查询时命中率为 null，而真实 QPS 可以为 0。
- `/metrics` 的每条指标还带 `available`、`unavailable_reason`、`source`、`layer` 和 `unit`。缺失指标不应在外部平台补零，也不应在图表中跨故障时段连线。
- DNS 请求完成可返回 HTTP 200，同时 `available=false` 和 NXDOMAIN/SERVFAIL。HTTP 故障与 DNS 协议结果分别处理。

## 配置与维护任务

配置保存与单文件恢复仍执行白名单、大小和内容校验，并记录历史。返回的 `verified=false` 明确表示未证明每条规则生效。面板中的配置中心提供历史版本、有限逐行差异、下载和恢复确认；大文件通过原文或下载核对。

先读取 `config/apply-plan`，再把计划的 `sha256` 填入任务 `expected_sha256`。输入变化导致计划失效，必须重新预览。快照任务的 `expected_sha256` 必须为 null。请求体中的 `request_id` 是 32 位小写十六进制幂等编号，与 HTTP 追踪编号不同；网络重试必须复用。

`expected_sha256` 不是文件保存的并发版本检查：当前文件保存 `{content}` 不支持客户端提交并在源端锁内检查的 `expected_revision` / `If-Match`。首期 dns 只读接入与配置深链不扩大这部分权限；跨平台配置编辑需要另行设计条件保存契约。

提交返回 HTTP 202 和任务 ID；通过 `GET /maintenance/tasks/{task_id}` 跟踪 `queued → running → succeeded/failed/interrupted`。管理服务重启后未完成任务标记 interrupted，禁止自动重放；相同幂等编号会返回原任务。应用完成可为 `applied_unverified`，仍应另行查询目标域名验证规则。

最近列表最多返回 20 项，日志最多保留 100 个任务记录；超过保留数量的任务查询返回 404。任务错误和回退结果保留在 `result` 中，HTTP 查询成功并不代表任务成功。实例 ID 存于状态卷；同卷重启保持 ID，删除状态卷会得到新 ID，克隆状态卷时应由外部平台处理重复身份。

## 接入示例

```sh
curl -H "Authorization: Bearer $SUITE_READ_TOKEN" \
  -H "X-Request-ID: home-check-0001" \
  http://127.0.0.1:8080/api/v1/metrics

curl -H "Authorization: Bearer $SUITE_READ_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"domain":"example.com","record_type":"A","server":"127.0.0.1","port":53}' \
  http://127.0.0.1:8080/api/v1/dns-test
```

自定义端口时，请先读 `capabilities.dns_defaults` 后显式传入查询目标。后端接受的地址是从集成容器网络访问的地址。自动化配置变更应使用独立管理凭据，只读采集不需要该权限。

## 面向家庭网络平台的接入建议

外部平台先调用 `instance` 和 `capabilities` 建立设备清单，再定时读取 `runtime` 或 `metrics`。DNS 故障告警触发时调用 `dns-test` 和 `health-check`，保存请求编号、实例 ID、采样时间、RCODE 和目标记录。配置编辑可深链至本地面板的 `#/config`、`#/lists`、`#/ttl` 或 `#/advanced`；外部平台无需重复实现全部编辑器。

RouterOS 适配器应独立于 PaoPaoDNS API：由家庭网络平台的服务端保存 ROS 凭据，通过 RouterOS 官方 API 或 REST API 读取接口、地址、DHCP、路由、连接跟踪和系统健康信息，再按时间戳与 PaoPaoDNS 的实例指标关联。浏览器不应直接连接 RouterOS，也不应让 PaoPaoDNS 容器保存主路由凭据。

首期 API 不承诺 RouterOS 版本、字段映射或写操作。接入 RouterOS 前需要锁定 RouterOS 主版本、启用的 API 服务、TLS/证书方式和首批只读指标，并为断线、权限不足和设备重启单独建模。
