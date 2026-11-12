# PPGW 问题报告：空闲连接快照为 null 时，观测 API 错误返回 502

日期：2026-11-07。用途：提交给 PPGW / LovestGateway 维护方分析。

建议 Issue 标题：`[Observer API] /api/v1/mihomo/connections rejects a null empty connections snapshot with 502`

## 问题摘要

在 `suburl` 模式、连接快照为空的测试场景中，PPGW 的 `CoreClient.request('/connections')` 成功返回对象，但其中 `connections` 字段为 `null`。观测 API 仅接受列表，因此将该响应拒绝为 HTTP 502、错误码 `INVALID_CORE_RESPONSE`。LovestDNS 联动页据此显示读取失败，无法正常呈现空快照。

**归因边界：**本报告确认了 PPGW 内部 `/connections` 的响应和观测适配代码行为；未绕过 PPGW 直接抓取 Mihomo 原生控制接口响应。此前口头描述“Mihomo 空闲时返回 null”应理解为此接口链上的观测结果，`null` 究竟由 Mihomo 还是 PPGW 转接层产生，仍请维护方核对。

## 已验证环境

| 项目 | 记录 |
| --- | --- |
| 网关部署 | Debian amd64 原生 systemd 部署 |
| 网关运行模式 | `suburl` |
| 观测 API | 已启用，使用既有只读 observer Token |
| 首次发现的构建 | LovestGateway `0.2.2`，提交 `4ac94e30a69e968e9496241bacf53ec241ed36df` |
| 后续收尾构建 | LovestGateway `0.2.3`，提交 `0a77e5162d22b1996b74a276d55c6e07f72ec33c` |
| 消费方 | LovestDNS `0.1.0-dev.lovestdns-native.20261107.console3` |

上述版本用于定位现场构建，不代表所有同版本安装都必然复现。0.2.3 收尾时仍核对到未修正的观测代码，随后在测试实例补入兼容修正。

未修正的已安装 `gateway_api.py` SHA-256：

```text
3f54556dfbc1eefd90c9feb465743053bd8feacb51040fa3890b143dfad72b05
```

未记录本次运行的 Mihomo 二进制准确版本；请维护方结合构建清单补充。报告不包含 Token、会话、订阅或实际客户端连接明细。

## 复现步骤与实际结果

1. 使用已启用观测 API 的 PPGW，在 `suburl` 模式下等待连接快照为空。
2. 通过网关既有内部 `CoreClient` 读取 `/connections`。
3. 调用只读观测接口 `GET /api/v1/mihomo/connections?limit=100`。
4. 比较空快照的表示与 HTTP 状态。

实测链路：

```text
CoreClient -> GET /connections
  HTTP 请求成功，解析后的对象中 connections 为 None（JSON null）

PPGW -> GET /api/v1/mihomo/connections?limit=100
  HTTP 502
  error.code = INVALID_CORE_RESPONSE

LovestDNS -> GET /api/v1/gateway/connections?view=mihomo
  HTTP 502，界面提示上游读取失败
```

同期实例身份、能力、运行状态、近期连接、持久历史及 FakeIP 查询可正常响应。因此故障范围已缩小到 Mihomo 快照的空值处理，并非整个观测 API 无法连接或只读 Token 失效。

### 同机只读诊断命令

以下命令在网关主机执行，使用已有凭据且仅输出响应类型、状态和记录数；不输出凭据或连接内容，不触发主动出站探测。需有权限读取网关本地配置。

```sh
sudo python3 - <<'PY'
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

sys.path.insert(0, '/usr/local/lib/paopaogw')
from gateway_api import CoreClient, NoRedirect, load_settings

core = CoreClient(Path('/var/lib/paopaogw/core-token'))
rows = core.request('/connections').get('connections')
print(json.dumps({
    'core_connections_type': type(rows).__name__,
    'core_connections_is_null': rows is None,
    'core_connections_count': len(rows) if isinstance(rows, list) else None,
}))

settings = load_settings(Path('/etc/paopaogw/api.json'))
host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(settings.listen, settings.listen)
authority = '[' + host + ']' if ':' in host else host
url = f'http://{authority}:{settings.port}/api/v1/mihomo/connections?limit=100'
req = Request(url, headers={'Authorization': 'Bearer ' + settings.read_token})
opener = build_opener(ProxyHandler({}), NoRedirect())
try:
    response = opener.open(req, timeout=10)
except HTTPError as error:
    response = error
with response:
    status = response.status
    raw = response.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise RuntimeError('Response too large; content omitted')
    body = json.loads(raw)
items = body.get('data', {}).get('items')
print(json.dumps({
    'observer_http': status,
    'observer_error_code': body.get('error', {}).get('code'),
    'observer_items_count': len(items) if isinstance(items, list) else None,
}))
PY
```

未修正且空快照为 `null` 时，预期复现输出为 `core_connections_is_null=true`、`observer_http=502`、`observer_error_code=INVALID_CORE_RESPONSE`。若当时存在连接，或已包含修正，可能不会复现。

## 代码原因

位置：PPGW 仓库 `integration/gateway_api.py`，`Application.dispatch()` 中 `/api/v1/mihomo/connections` 分支；安装位置为 `/usr/local/lib/paopaogw/gateway_api.py`。

未修正代码节选：

```python
result = self.core.request('/connections')
rows = result.get('connections', [])
if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
    raise ApiError(502, 'INVALID_CORE_RESPONSE')
```

`dict.get(key, [])` 只在字段不存在时使用默认值。字段存在且值为 `None` 时，`rows` 仍为 `None`，随后被类型检查拒绝。

`yaml` 模式也进入同一代码分支，因此从代码看可能有相同风险；本次实机验证的是 `suburl`，未单独做 `yaml` 实机复现。

## 建议行为与已验证的最小修正

建议先确认内部接口契约是否允许用 `null` 表示空连接。如果允许，可在观测适配层只规范这个空值；若契约规定必须返回数组，也可以在产生响应的上游统一输出 `[]`，再保留必要的兼容处理。

测试实例已采用下列最小修改：

```diff
 result = self.core.request('/connections')
 rows = result.get('connections', [])
+# Mihomo returns null when the current snapshot is empty.
+if rows is None: rows = []
 if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
     raise ApiError(502, 'INVALID_CORE_RESPONSE')
```

注释中的来源表述也建议维护方按最终定位修订。不要用 `rows = value or []` 无差别兜底，否则 `False`、`0`、空字符串和空对象等错误类型也会被掩盖；网络失败、认证失败同样不能转换为空列表。

正常空快照应返回 HTTP 200，响应节选为：

```json
{
  "data": {
    "items": [],
    "source": "mihomo_connection_snapshot",
    "complete_history": false,
    "source_age_seconds": null,
    "remote_target_verified": false
  }
}
```

空快照只表示本次返回没有记录，不证明没有流量、历史完整或终端端到端转发成功。采样年龄未知时应继续保留未知语义。

## 回归要求与验证结果

| 核心返回的 connections | 应有结果 | 本次验证 |
| --- | --- | --- |
| `null` | HTTP 200、`items=[]` | 通过 |
| `[]` | HTTP 200、`items=[]` | 通过 |
| 合法对象列表 | 保留规则、连接链及允许公开的字段 | 既有用例通过 |
| `false`、`0`、`""`、`{}`、`[null]` | 继续拒绝为 `INVALID_CORE_RESPONSE` | 通过 |
| 非 yaml/suburl 模式 | 保留 `MIHOMO_NOT_ACTIVE` | 既有用例通过 |

本地 `tests/test_gateway_api.py` 已有新增用例 `test_mihomo_empty_snapshot_accepts_null_but_rejects_malformed_values`；该文件共 21 项测试通过。

实机修正后：近期连接、持久历史、Mihomo 空快照及 FakeIP 均通过真实 HTTP 读取；浏览器完成三种视图、记录详情、独立入口、390px 布局及无后台轮询检查，无 JavaScript 运行异常。修正时仅重启观测 API，转发核心进程保持不变，配置与凭据保持一致。

修正后已安装文件 SHA-256：

```text
7355c2cdc2bf372c91f33e0dc1c05b2b9e9d4b4616f3b3d4cc714bd7ba2fdd89
```

## 需要维护方进一步分析的范围

- 确认 `/connections` 的 `null` 来源及正式契约，补录实际 Mihomo 版本。
- 将修正与回归纳入正式源码和安装包，核对安装产物实际包含修改；当前测试机补丁不能代替正式发布验收。
- 检查 `integration/observe.py` 的历史采集分支：它也会遍历 `.get('connections', [])`，若为 `None`，从代码看会触发 `TypeError`，随后被外层异常处理忽略。此为代码审查发现的相关风险，本次未独立量化对持久采集的影响。
- 验证空闲与有连接状态切换，以及 `yaml`、`suburl` 两种模式，避免只在 UI 将错误隐藏为空列表。

## 当前状态

测试实例已补入兼容修正，保留原文件备份。本报告未据此宣称 PPGW 的所有 0.2.3 安装包已修复，也未在本任务生成新的 PPGW 正式发布包。维护方合入并发布后，应以新包重新验收，避免升级覆盖现场修正。
