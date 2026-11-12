"""Concrete OpenAPI 3.1 schemas, shared by docs and response contract tests."""
import re
from api_v1 import OPERATIONS, READ_OPERATIONS


def ref(name):
    return {"$ref": "#/components/schemas/" + name}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def array(items, **kwargs):
    return {"type": "array", "items": items, **kwargs}


def dictionary(values):
    return {"type": "object", "additionalProperties": values}


def openapi_document(version):
    text = {"type": "string"}
    boolean = {"type": "boolean"}
    number = {"type": "number"}
    nonnegative = {"type": "number", "minimum": 0}
    integer = {"type": "integer", "minimum": 0}
    nullable_number = {"type": ["number", "null"], "minimum": 0}
    nullable_text = {"type": ["string", "null"]}
    timestamp = {"type": "number", "description": "Unix UTC 时间戳，单位秒，可含小数"}
    port = {"type": "integer", "minimum": 1, "maximum": 65535}
    digest = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    nullable_digest = {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"}
    file_version = {"type": "string", "pattern": "^(?:[a-f0-9]{64}|missing)$"}
    record_types = ["A", "AAAA", "CNAME", "NS", "MX", "TXT", "SRV", "PTR", "SVCB", "HTTPS"]
    states = ["queued", "running", "succeeded", "failed", "interrupted"]
    conditions = {"auto_reload": boolean, "requires_reload": boolean, "requires_restart": boolean,
                  "watched_now": boolean, "condition": text, "verified": {"type": "boolean", "const": False}}
    schemas = {
        "Meta": obj({"api_version": {"type": "string", "const": "1"}, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,64}$"}}, ["api_version", "request_id"]),
        "Error": obj({"error": obj({"code": {"type": "string", "enum": ["INVALID_ARGUMENT", "UNAUTHORIZED", "FORBIDDEN", "API_DISABLED", "NOT_FOUND", "METHOD_NOT_ALLOWED", "CONFLICT", "PRECONDITION_FAILED", "PAYLOAD_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE", "UNAVAILABLE", "SAVE_UNCONFIRMED", "STALE_DATA", "INTERNAL_ERROR"]}, "message": text}, ["code", "message"]), "meta": ref("Meta")}, ["error", "meta"]),
        "Instance": obj({"id": {"type": "string", "format": "uuid", "description": "状态卷持久身份；Web/容器同卷重启保留，删除状态卷生成新身份，克隆状态卷会复制身份"}, "name": text, "kind": {"const": "paopaodns"}, "edition": {"const": "integrated"}, "version": text}, ["id", "name", "kind", "edition", "version"]),
        "CoreBuild": obj({"image": text, "source_commit": text, "components": {"anyOf": [{"type": "null"}, obj({"selected": array(text), "versions": dictionary(text), "lock_sha256": digest}, ["selected", "versions", "lock_sha256"])]}}, ["image", "source_commit"]),
        "InstallationReceipt": obj({'status': {'enum': ['missing', 'mismatch', 'recorded']}, 'archive_sha256': nullable_digest,
            'manifest_sha256': nullable_digest, 'installed_at': nullable_number}, ['status', 'archive_sha256', 'manifest_sha256', 'installed_at']),
        "Installation": obj({'source': {'enum': ['unavailable', 'native_service_environment']},
            **{key: nullable_text for key in ('service_name', 'control_command', 'config_file', 'data_dir', 'state_dir', 'release_path')},
            'receipt': ref('InstallationReceipt')}, ['source', 'service_name', 'control_command', 'config_file', 'data_dir', 'state_dir', 'release_path', 'receipt']),
        "Provenance": obj({'source': {'const': 'installed_metadata'},
            'application': obj({'source_commit': nullable_text, 'source_sha256': nullable_digest, 'method': nullable_text}, ['source_commit', 'source_sha256', 'method']),
            'frontend': obj({'source_commit': nullable_text, 'source_sha256': nullable_digest, 'manifest_sha256': nullable_digest,
                'dependency_mode': nullable_text, 'working_tree_modified': {'type': ['boolean', 'null']}},
                ['source_commit', 'source_sha256', 'manifest_sha256', 'dependency_mode', 'working_tree_modified']),
            'base_package': obj({'version': nullable_text, 'archive_sha256': nullable_digest}, ['version', 'archive_sha256']),
            'process_files_verified': {'const': False}}, ['source', 'application', 'frontend', 'base_package', 'process_files_verified']),
        "Capabilities": obj({"edition": {"const": "integrated"}, "version": text, "api_version": {"const": "1"}, "instance_id": {"type": "string", "format": "uuid"}, "name": text,
            "access_role": {"enum": ["admin", "observer"]}, "core": ref("CoreBuild"), "deployment": {"enum": ["native", "container"]},
            "installation": ref("Installation"), "provenance": ref("Provenance"),
            "dns_defaults": obj({"server": text, "port": port}, ["server", "port"]),
            "record_types": array({"enum": record_types}), "features": dictionary(boolean), "runtime_error": nullable_text,
            "maintenance": obj({"available": boolean, "phase": text}, ["available", "phase"]),
            "permissions": obj({key: boolean for key in ("observe", "diagnose", "configure", "maintain")}, ["observe", "diagnose", "configure", "maintain"])},
            ["edition", "version", "api_version", "instance_id", "name", "access_role", "core", "dns_defaults", "record_types", "features", "permissions"]),
        "Freshness": obj({"age_seconds": nonnegative, "max_age_seconds": {"type": "integer", "const": 25}}, ["age_seconds", "max_age_seconds"]),
        "Component": obj({"statename": text, "pid": integer, "start": timestamp, "exitstatus": {"type": "integer"}}, ["statename", "pid", "start", "exitstatus"]),
        "Layer": obj({"available": boolean, "counters": dictionary(nonnegative), "qps": nullable_number,
            "cache_hit_ratio": {"type": ["number", "null"], "minimum": 0, "maximum": 1}, "comparable": boolean,
            "comparison_reason": {"enum": ["comparable", "first_sample", "clock_changed", "process_changed", "missing_counters", "counters_reset"]},
            "window_seconds": nullable_number, "error": text}, ["available"]),
        "RedisInfo": obj({**{key: integer for key in ["used_memory", "maxmemory", "evicted_keys", "keyspace_hits", "keyspace_misses", "loading", "rdb_last_save_time", "rdb_changes_since_last_save", "rdb_bgsave_in_progress"]},
            "rdb_last_bgsave_status": text, "redis_version": text,
            **{key: obj({"keys": integer, "expires": integer, "avg_ttl": integer}) for key in ("db0", "db1")}}),
        "Redis": obj({"available": boolean, "info": ref("RedisInfo"), "error": text}, ["available"]),
        "Persistence": obj({"scope": text, "last_save_time": nullable_number, "last_save_status": nullable_text,
            "saving": nullable_number, "loading": nullable_number, "changes_since_save": nullable_number,
            "rdb_bytes": nullable_number, "disk_free_bytes": nonnegative, "storage_error": text, "warnings": array(text)}, ["scope", "warnings"]),
        "Runtime": obj({"schema_version": {"const": 1}, "sampled_at": timestamp, "interval_seconds": nonnegative, "ready": boolean,
            "core_healthy": boolean, "components": dictionary(ref("Component")), "layers": dictionary(ref("Layer")),
            "redis": ref("Redis"), "persistence": ref("Persistence"), "freshness": ref("Freshness"), "dns": ref("DnsListeners")},
            ["schema_version", "sampled_at", "ready", "core_healthy", "components", "layers", "freshness"]),
        "Metric": obj({"name": {"enum": ["qps", "cache_hit_ratio", "used_memory"]}, "source": {"enum": ["unbound", "redis"]},
            "layer": {"enum": ["recursive", "forward", "shared"]}, "unit": {"enum": ["queries/second", "ratio", "bytes"]},
            "value": nullable_number, "available": boolean, "unavailable_reason": nullable_text, "window_seconds": nullable_number},
            ["name", "source", "layer", "unit", "value", "available", "unavailable_reason", "window_seconds"]),
        "Metrics": obj({"sampled_at": timestamp, "interval_seconds": nonnegative, "freshness": ref("Freshness"), "metrics": array(ref("Metric"))}, ["sampled_at", "interval_seconds", "freshness", "metrics"]),
        "Answer": obj({"name": text, "type": text, "class": integer, "ttl": {"type": "integer", "minimum": 0, "maximum": 4294967295}, "value": text}, ["name", "type", "ttl", "value"]),
        "DnsResult": obj({"available": boolean, "responded": boolean, "domain": text, "record_type": {"enum": record_types}, "server": text, "port": port,
            "answers": array(ref("Answer")), "authority": array(ref("Answer")), "additional": array(ref("Answer")), "results": array(text), "transport": {"enum": ["udp", "tcp"]}, "tcp_fallback": boolean, "udp_truncated": boolean,
            "checked_at": {"type": "string", "format": "date-time"}, "elapsed_ms": nonnegative,
            "status": {"enum": ["ok", "no_records", "dns_error", "incomplete", "timeout", "network_error", "invalid_response", "invalid_input", "probe_error"]},
            "error": text, "rcode": text, "rcode_value": integer, "truncated": boolean, "authoritative": boolean, "recursion_available": boolean,
            "counts": obj({key: integer for key in ("questions", "answers", "authority", "additional")}),
            "debug": array(obj({"name": text, "value": text, "ttl": integer}, ["name", "value", "ttl"]))},
            ["available", "responded", "domain", "record_type", "server", "port", "answers", "results", "transport", "tcp_fallback", "udp_truncated", "checked_at", "elapsed_ms", "status"]),
        "DnsReport": obj({"schema_version": {"const": 1}, "kind": {"const": "paopaodns-dns-report"}, "started_utc": {"type": "string", "format": "date-time"},
            "method": text, "tests": array(obj({"label": text, "result": ref("DnsResult")}, ["label", "result"]), minItems=1, maxItems=4)}, ["schema_version", "kind", "started_utc", "method", "tests"]),
        "DnsHealth": obj({"pass": boolean, "server": text, "port": port, "error": text,
            "tests": dictionary(obj({"domain": text, "resolved": boolean, "result": ref("DnsResult")}, ["domain", "resolved"]))}, ["pass", "server", "port", "tests"]),
        "File": obj({"revision": digest, "filename": text, "content": text, **conditions}, ["revision", "filename", "content", *conditions]),
        "SavedFile": obj({"revision": digest, "filename": text, "message": text, **conditions}, ["revision", "filename", "message", *conditions]),
        "Status": obj({"data_dir": text, "data_readable": boolean, "data_writable": boolean, "auth_enabled": boolean,
            "env": dictionary(text), "files": dictionary(boolean), "files_info": dictionary(obj({"exists": boolean, **conditions}, ["exists", *conditions]))},
            ["data_dir", "data_readable", "data_writable", "auth_enabled", "env", "files", "files_info"]),
        "Env": dictionary(text),
        "Revision": obj({"id": integer, "created_at": timestamp, "sha256": digest, "bytes": integer, "reason": text}, ["id", "created_at", "sha256", "bytes", "reason"]),
        "History": obj({"filename": text, "revisions": array(ref("Revision"), maxItems=20)}, ["filename", "revisions"]),
        "RevisionContent": obj({"filename": text, "revision": integer, "content": text}, ["filename", "revision", "content"]),
        "ApplyPlan": obj({"sha256": digest, "inputs": dictionary(digest), "candidate_sha256": digest, "candidate": text, "notice": text}, ["sha256", "inputs", "candidate_sha256", "candidate", "notice"]),
        "TaskResult": obj({"message": text, "error": text, "state": {"enum": ["applied_unverified", "restarted_unverified", "saved"]}, "verified": boolean,
            "input_sha256": digest, "runtime_sha256": digest, "saved_at": timestamp, "bytes": integer, "changes_after_save": nullable_number,
            "probe": obj({"responded": boolean, "rcode": integer, "scope": text}, ["responded", "rcode"]),
            "rollback": obj({"attempted": boolean, "runtime_restored": boolean, "service_running": boolean, "error": text}, ["runtime_restored", "service_running"])}),
        "Task": obj({"id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}, "action": {"enum": ["apply_custom_mod", "save_redis_snapshot", "restart_dns_core"]},
            "expected_sha256": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$"}, "state": {"enum": states}, "created": timestamp, "updated": timestamp,
            "result": {"anyOf": [ref("TaskResult"), {"type": "null"}]}}, ["id", "action", "state", "created", "updated", "result"]),
        "Tasks": obj({"tasks": array(ref("Task"), maxItems=20)}, ["tasks"]),
        "TaskResponse": obj({"task": ref("Task")}, ["task"]),
        "Logs": obj({"component": text, "lines": array(text, maxItems=200), "error": text}, ["component", "lines"]),
        "Query": obj({"domain": {"type": "string", "maxLength": 253, "example": "example.com"}, "record_type": {"enum": record_types, "default": "A"},
            "server": text, "port": port, "transport": {"enum": ["udp", "tcp"], "default": "udp"}}, ["domain"]),
        "Target": obj({"label": {"type": "string", "minLength": 1, "maxLength": 40}, "server": text, "port": port, "transport": {"enum": ["udp", "tcp"]}}, ["server", "port"]),
        "TaskSubmission": {**obj({"action": {"enum": ["apply_custom_mod", "save_redis_snapshot", "restart_dns_core"]}, "request_id": {"type": "string", "pattern": "^[a-f0-9]{32}$", "description": "幂等编号。网络重试复用；与 X-Request-ID 不同"},
            "expected_sha256": {"type": ["string", "null"], "pattern": "^[a-f0-9]{64}$", "description": "应用时为预览 sha256，快照和重启必须为 null；不是文件保存的并发版本检查"}}, ["action", "request_id", "expected_sha256"]), "additionalProperties": False},
    }
    schemas['DnsListenerEndpoint'] = obj({'address': text, 'port': port, 'transport': {'enum': ['udp', 'tcp']},
        'family': {'enum': [4, 6]}, 'wildcard': boolean}, ['address', 'port', 'transport', 'family', 'wildcard'])
    schemas['DnsListenerProcess'] = obj({'available': boolean, 'reason': {'enum': [None, 'not_running', 'unconfirmed']},
        'pid': integer, 'start_ticks': text, 'endpoints': array(ref('DnsListenerEndpoint'))}, ['available', 'reason', 'endpoints'])
    schemas['DnsListeners'] = obj({'source': {'const': 'linux_process_sockets'}, 'scope': {'const': 'service_network_namespace'},
        'mode': {'enum': ['automatic', 'manual', 'unknown']}, 'entry_component': {'enum': [None, 'mosdns', 'unbound-raw']},
        'client_path_verified': {'const': False}, 'listeners': dictionary(ref('DnsListenerProcess'))},
        ['source', 'scope', 'mode', 'entry_component', 'client_path_verified', 'listeners'])
    schemas['ConfigStateRow'] = obj({'filename': text, 'revision': file_version, 'state': {'enum': ['unknown', 'recorded', 'changed']},
        'recorded_revision': {'anyOf': [file_version, {'type': 'null'}]}, 'recorded_at': nullable_number, 'action': nullable_text, 'verified': {'const': False}},
        ['filename', 'revision', 'state', 'recorded_revision', 'recorded_at', 'action', 'verified'])
    schemas['ConfigState'] = obj({'files': array(ref('ConfigStateRow')), 'sampled_at': timestamp, 'scope': text, 'verified': {'const': False}},
                                ['files', 'sampled_at', 'scope', 'verified'])
    schemas["Comparison"] = obj({"domain": schemas["Query"]["properties"]["domain"], "record_type": schemas["Query"]["properties"]["record_type"], "targets": array(ref("Target"), minItems=1, maxItems=4)}, ["domain", "targets"])
    output = {"config_state": "ConfigState", "instance": "Instance", "capabilities": "Capabilities", "runtime": "Runtime", "metrics": "Metrics", "status": "Status", "env": "Env", "file_read": "File", "file_write": "SavedFile", "dns_query": "DnsResult", "dns_compare": "DnsReport", "dns_health": "DnsHealth", "apply_plan": "ApplyPlan", "task_list": "Tasks", "task_submit": "TaskResponse", "task_read": "TaskResponse", "history_list": "History", "history_read": "RevisionContent", "history_restore": "SavedFile", "logs": "Logs"}
    for name in set(output.values()):
        schemas[name + "Response"] = obj({"data": ref(name), "meta": ref("Meta")}, ["data", "meta"])
    operations = OPERATIONS + [("/instance", "GET", "instance", None, "持久实例身份"), ("/capabilities", "GET", "capabilities", None, "版本、权限与能力"),
        ("/runtime", "GET", "runtime", None, "当前分层运行快照"), ("/metrics", "GET", "metrics", None, "带单位和来源的指标"), ("/openapi.json", "GET", "schema", None, "OpenAPI 契约")]
    paths = {}
    for route, method, operation, _, title in operations:
        route = re.sub(r"<(?:int:)?(\w+)>", r"{\1}", route)
        parameters = [{"name": "X-Request-ID", "in": "header", "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,64}$"}}]
        for name in re.findall(r"{(\w+)}", route):
            parameters.append({"name": name, "in": "path", "required": True, "schema": integer if name == "revision" else text})
        if operation in {'file_write', 'history_restore'}:
            parameters += [{'name': 'If-Match', 'in': 'header', 'schema': text, 'description': '单个引号包围的强SHA-256版本；在共享写锁内检查'},
                           {'name': 'If-None-Match', 'in': 'header', 'schema': {'const': '*'}, 'description': '仅当文件不存在时创建'}]
        if operation == "dns_health":
            parameters += [{"name": name, "in": "query", "schema": prop} for name, prop in (("server", text), ("port", port))]
        success = "202" if operation == "task_submit" else "200"
        paths.setdefault(route, {})[method.lower()] = endpoint = {"operationId": operation, "summary": title,
            "description": "管理或只读 Token" if operation in READ_OPERATIONS else "仅管理 Token", "parameters": parameters, "security": [{"BearerToken": []}],
            "x-allowed-roles": ["admin", "observer"] if operation in READ_OPERATIONS else ["admin"],
            "responses": {success: {"description": "DNS 查询完成请检查 available/status；任务完成不代表规则已验证", "headers": {"X-Request-ID": {"schema": text}},
                "content": {"application/json": {"schema": ref(output[operation] + "Response") if operation != "schema" else {"type": "object"}}}},
                **{str(code): {"description": label, "content": {"application/json": {"schema": ref("Error")}}} for code, label in [(400, "输入无效"), (401, "未认证"), (403, "权限不足"), (404, "对象不存在或已过保留期"), (405, "方法不支持"), (409, "冲突"), (412, "文件版本已改变"), (413, "内容过大"), (415, "内容类型无效"), (500, "服务异常"), (503, "采集/维护服务不可用或样本过期")]}}
        }
        for response in endpoint["responses"].values():
            response["headers"] = {"X-Request-ID": {"schema": schemas["Meta"]["properties"]["request_id"]},
                                   "Cache-Control": {"schema": {"type": "string", "const": "no-store"}}}
        endpoint["responses"]["503"]["description"] = "UNAVAILABLE：采集/维护服务不可用；STALE_DATA：样本年龄大于 25 秒、未来时间或无效时间戳。无 data 字段。"
        endpoint['responses']['403']['description'] = 'FORBIDDEN：权限不足；API_DISABLED：外部 Token 接入已关闭。默认关闭，可在网页服务信息开启。'
        if operation in {"file_read", "file_write", "history_restore"}:
            endpoint['responses'][success]['headers']['ETag'] = {'description': '当前文件原始字节的强版本', 'schema': {'type': 'string', 'pattern': '^"[a-f0-9]{64}"$'}}
        if operation in {"file_write", "history_restore"}:
            endpoint['parameters'] += [
                {'name': 'If-Match', 'in': 'header', 'description': '单个读取时返回的强 ETag；与正文条件同时传入时必须一致', 'schema': {'type': 'string', 'pattern': '^"[a-f0-9]{64}"$'}},
                {'name': 'If-None-Match', 'in': 'header', 'description': '仅创建尚不存在的文件；不可与 If-Match 同用', 'schema': {'const': '*'}}]
        body = {"dns_query": ref("Query"), "dns_compare": ref("Comparison"), "task_submit": ref("TaskSubmission"), "file_write": obj({"content": text, "expected_revision": file_version}, ["content"]), "history_restore": obj({"expected_revision": file_version})}.get(operation)
        if body:
            endpoint["requestBody"] = {"required": operation != "history_restore", "content": {"application/json": {"schema": body}}}
    # Browser-only adapter; bearer credentials never authorize these operations.
    gateway_types = {
        'GatewayConfig': obj({'revision': text, 'enabled': boolean, 'has_token': boolean, **{k: nullable_text for k in ('url', 'instance_id', 'name', 'version')}}, ['revision', 'enabled', 'has_token']),
        'GatewaySummary': obj({'instance': obj({'id': text, 'name': nullable_text, 'version': nullable_text, 'kind': nullable_text}, ['id']), 'features': dictionary(boolean), 'mode': nullable_text, 'runtime': obj({'status': nullable_text, 'core_responding': boolean, 'outbound_reachable': nullable_text, 'forwarding_verified': boolean}), 'freshness': obj({'stale': boolean, 'age_seconds': nonnegative, 'max_age_seconds': nonnegative}), 'sampled_at': nullable_number, 'fetched_at': nullable_number}, ['instance', 'features', 'mode', 'runtime', 'freshness']),
        'GatewayConnections': obj({'items': array(obj({k: nullable_text for k in ('id', 'source', 'domain', 'client', 'destination')})), 'view': text, 'scope': text, 'filter_scope': text, 'complete_history': {'const': False}}, ['items', 'view', 'scope', 'filter_scope', 'complete_history']),
        'GatewayFakeIP': obj({**{k: nullable_text for k in ('status', 'source', 'domain', 'fakeip', 'cidr', 'validity')}, 'historical_ownership_verified': boolean, 'sampled_at': nullable_number, 'fetched_at': nullable_number}),
    }
    schemas.update(gateway_types)
    for name in gateway_types:
        schemas[name + 'Response'] = obj({'data': ref(name), 'meta': ref('Meta')}, ['data', 'meta'])
    gateway_ops = [('config', 'get', 'gateway_config', 'GatewayConfig'), ('config', 'put', 'gateway_save', 'GatewayConfig'), ('config', 'delete', 'gateway_disconnect', 'GatewayConfig'), ('test', 'post', 'gateway_test', 'GatewaySummary'), ('summary', 'get', 'gateway_summary', 'GatewaySummary'), ('connections', 'get', 'gateway_connections', 'GatewayConnections'), ('fakeip', 'get', 'gateway_fakeip', 'GatewayFakeIP')]
    for route, method, operation, schema in gateway_ops:
        endpoint = {'operationId': operation, 'description': 'Browser administrator session only; writes require CSRF. Optional, no background polling.', 'security': [{'BrowserSession': []}], 'x-allowed-roles': ['browser_admin'], 'responses': {'200': {'description': 'Success', 'content': {'application/json': {'schema': ref(schema + 'Response')}}}}}
        endpoint['responses'].update({str(code): {'description': 'Request rejected or gateway unavailable', 'content': {'application/json': {'schema': ref('Error')}}} for code in (400, 401, 403, 409, 502, 503)})
        for response in endpoint['responses'].values():
            response['headers'] = {'X-Request-ID': {'schema': schemas['Meta']['properties']['request_id']},
                                   'Cache-Control': {'schema': {'type': 'string', 'const': 'no-store'}}}
        if operation in ('gateway_save', 'gateway_test'):
            body = obj({'url': text, 'token': {'type': 'string', 'writeOnly': True}, 'expected_revision': text}, ['url'] + (['expected_revision'] if operation == 'gateway_save' else []))
            endpoint['requestBody'] = {'required': True, 'content': {'application/json': {'schema': body}}}
        elif operation == 'gateway_disconnect': endpoint['requestBody'] = {'required': True, 'content': {'application/json': {'schema': obj({'expected_revision': text}, ['expected_revision'])}}}
        paths.setdefault('/gateway/' + route, {})[method] = endpoint
    return {"openapi": "3.1.0", "info": {"title": "LovestDNS Integration API", "version": version, "description": "DNS 专用接口。Unix 时间戳单位秒；interval_seconds 为目标采集周期，window_seconds 为实际比较窗口。过期数据返回 503。详见 docs/integration-api.md。"}, "servers": [{"url": "/api/v1"}], "paths": paths, "components": {"securitySchemes": {"BrowserSession": {"type": "apiKey", "in": "cookie", "name": "lovestdns_session"}, "BearerToken": {"type": "http", "scheme": "bearer"}}, "schemas": schemas}}
