"""Stable integration boundary over the same services used by the legacy API."""
import hmac
from contextlib import closing
import logging
import math
import os
import re
import sqlite3
import time
import uuid

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import HTTPException
from browser_auth import current_session, csrf_valid, external_api_enabled, WRITE_METHODS


# Only these operations are exposed to the external observer credential.
READ_OPERATIONS = {"instance", "capabilities", "runtime", "metrics", "schema",
                   "dns_query", "dns_compare", "dns_health"}
LOGGER = logging.getLogger("paopaodns.api")
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
ERROR_CODES = {400: "INVALID_ARGUMENT", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
               404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT",
               412: "PRECONDITION_FAILED", 413: "PAYLOAD_TOO_LARGE", 415: "UNSUPPORTED_MEDIA_TYPE", 503: "UNAVAILABLE"}

# Each endpoint delegates directly to the existing handler. Validation, locks,
# config history and task idempotency consequently have one implementation.
OPERATIONS = [
    ("/status", "GET", "status", "api_status", "读取配置文件状态与脱敏变量"),
    ("/env", "GET", "env", "api_env", "读取目标环境变量覆盖"),
    ("/file/<filename>", "GET", "file_read", "api_read_file", "读取白名单配置文件"),
    ("/file/<filename>", "PUT", "file_write", "api_write_file", "校验并保存配置，记录历史"),
    ("/dns-test", "POST", "dns_query", "api_dns_test", "查询 DNS 记录与诊断标记"),
    ("/dns-compare", "POST", "dns_compare", "api_dns_compare", "并行对照最多四条 DNS 链路"),
    ("/health-check", "GET", "dns_health", "api_health_check", "执行两个样本域名的 DNS 检查"),
    ("/config/state", "GET", "config_state", "configuration_state", "读取持久应用记录与当前配置版本"),
    ("/config/apply-plan", "GET", "apply_plan", "apply_plan", "预览当前配置生成结果与输入指纹"),
    ("/maintenance/tasks", "GET", "task_list", "maintenance_tasks", "读取最近维护任务"),
    ("/maintenance/tasks/<task_id>", "GET", "task_read", "maintenance_task", "按任务编号查询状态与结果"),
    ("/maintenance/tasks", "POST", "task_submit", "submit_task", "提交有幂等编号的维护任务"),
    ("/history/<filename>", "GET", "history_list", "list_history", "读取配置历史列表"),
    ("/history/<filename>/<int:revision>", "GET", "history_read", "read_history", "读取指定历史版本"),
    ("/history/<filename>/<int:revision>/restore", "POST", "history_restore", "restore_history", "重新校验并恢复单文件版本"),
    ("/core-logs/<component>", "GET", "logs", "core_logs", "读取最近二百行脱敏组件日志"),
    ("/query-recording", "GET", "query_recording_status", "query_recording_status", "查询采集状态与保留上限，仅管理权限"),
    ("/query-recording", "PUT", "query_recording_configure", "query_recording_configure", "启停被动查询采集，默认关闭"),
    ("/query-records", "GET", "query_records_recent", "query_records_recent", "读取最近真实入口查询，不推测缓存或分流路径"),
    ("/query-records", "DELETE", "query_records_clear", "query_records_clear", "清空持久查询记录并丢弃待采集事件"),
]


def instance_identity(root):
    """Persist once, safely shared by multiple Gunicorn workers."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # sqlite's transaction context commits/rolls back but does not close the
    # connection. Close explicitly across repeated requests and Web restarts.
    with closing(sqlite3.connect(root / "instance.sqlite", timeout=5)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO identity VALUES(1, ?)", (str(uuid.uuid4()),))
        return db.execute("SELECT value FROM identity WHERE singleton=1").fetchone()[0]


def metric_values(sample):
    """Small, documented semantic metric set; no inferred global DNS counters."""
    metrics = []
    for layer in ("recursive", "forward"):
        source = sample.get("layers", {}).get(layer, {})
        for name, unit in (("qps", "queries/second"), ("cache_hit_ratio", "ratio")):
            value = source.get(name) if source.get("available") and source.get("comparable") else None
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
            if name == "cache_hit_ratio" and valid:
                valid = value <= 1
            reason = ("component_unavailable" if not source.get("available") else
                      source.get("comparison_reason", "not_comparable") if not source.get("comparable") else
                      "no_queries" if name == "cache_hit_ratio" and value is None else "invalid_metric")
            metrics.append({"name": name, "source": "unbound", "layer": layer,
                            "unit": unit, "value": value if valid else None,
                            "available": bool(valid), "unavailable_reason": None if valid else reason,
                            "window_seconds": source.get("window_seconds") if source.get("comparable") else None})
    redis = sample.get("redis", {})
    value = redis.get("info", {}).get("used_memory") if redis.get("available") else None
    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    metrics.append({"name": "used_memory", "source": "redis", "layer": "shared",
                    "unit": "bytes", "value": value if valid else None, "available": bool(valid),
                    "unavailable_reason": None if valid else "component_unavailable", "window_seconds": None})
    return {"sampled_at": sample["sampled_at"], "interval_seconds": sample.get("interval_seconds", 5),
            "metrics": metrics}


def register(server):
    app, base = server.app, server.base
    api = Blueprint("v1", __name__, url_prefix="/api/v1")

    def meta():
        return {"api_version": "1", "request_id": g.request_id}

    def error(status, message, code=None):
        return jsonify({"error": {"code": code or ERROR_CODES.get(status, "INTERNAL_ERROR"),
                                  "message": message}, "meta": meta()}), status

    def invoke(view, **kwargs):
        if view is base.api_write_file and not isinstance(request.get_json(), dict):
            return error(400, "Expected a JSON object")
        response = app.make_response(view(**kwargs))
        body = response.get_json()
        if response.status_code >= 400:
            message = body.get("error", response.status) if isinstance(body, dict) else response.status
            wrapped = app.make_response(error(response.status_code, str(message)))
            if response.headers.get('Retry-After'):
                wrapped.headers['Retry-After'] = response.headers['Retry-After']
            return wrapped
        wrapped = jsonify({"data": body, "meta": meta()})
        if response.headers.get('ETag'):
            wrapped.headers['ETag'] = response.headers['ETag']
        return wrapped, response.status_code

    original_auth = base.auth_middleware

    def authenticate():
        # The same browser session powers the panel even with token access off.
        # Apply the switch to legacy routes too; they must not be a bypass.
        if request.path.startswith('/api/') and not request.path.startswith('/api/v1/'):
            if not external_api_enabled(server.STATE_DIR):
                return jsonify({'error': '外部 API 已关闭，请在服务信息中开启'}), 403
        if not request.path.startswith("/api/v1/"):
            return original_auth()
        raw_id = request.headers.get("X-Request-ID", "")
        g.request_id = raw_id if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", raw_id) else uuid.uuid4().hex
        g.request_started = time.monotonic()
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        read_token = os.environ.get("SUITE_READ_TOKEN", "")

        if auth and not external_api_enabled(server.STATE_DIR):
            return error(403, "外部 API 已关闭，请在服务信息中开启", "API_DISABLED")

        def matches(expected):
            return bool(expected and token) and hmac.compare_digest(token.encode(), expected.encode())

        if matches(base.WEB_UI_TOKEN):
            g.api_role = "admin"
        elif matches(read_token):
            g.api_role = "observer"
        elif not auth and (session := current_session(server.STATE_DIR)):
            if session["initial"]:
                return error(403, "请先修改初始管理员密码")
            if request.method in WRITE_METHODS and not csrf_valid(session):
                return error(403, "登录校验已失效，请刷新页面")
            g.api_role = "admin"
        elif not base.WEB_UI_TOKEN and base.WEB_UI_ALLOW_NO_AUTH and not token and external_api_enabled(server.STATE_DIR):
            g.api_role = "admin"
        else:
            return error(401, "请先登录，或提供有效的 API Token")
        operation = (request.endpoint or "").removeprefix("v1.")
        if not (request.endpoint or "").startswith("v1."):
            if request.routing_exception and request.routing_exception.code == 405:
                return error(405, "此 API 不支持该 HTTP 方法")
            return error(404, "API 路径不存在")
        if g.api_role == "observer" and operation not in READ_OPERATIONS:
            return error(403, "只读接入不能访问配置、日志或维护任务")

    # Replace, rather than append after the legacy guard: observer tokens must
    # work only on the new allowlist and can never authorize legacy admin routes.
    handlers = app.before_request_funcs[None]
    handlers[handlers.index(original_auth)] = authenticate

    @app.after_request
    def trace(response):
        if request.path.startswith("/api/v1/") and hasattr(g, "request_id"):
            response.headers["X-Request-ID"] = g.request_id
            response.headers["Cache-Control"] = "no-store"
            LOGGER.info("api request_id=%s role=%s method=%s route=%s status=%s elapsed_ms=%.1f",
                            g.request_id, getattr(g, "api_role", "anonymous"), request.method,
                            str(request.url_rule or "unknown"), response.status_code,
                            (time.monotonic() - g.request_started) * 1000)
        return response

    @api.errorhandler(HTTPException)
    def http_error(exc):
        return error(exc.code, exc.description, getattr(exc, 'api_code', None))

    @api.errorhandler(Exception)
    def unexpected_error(exc):
        app.logger.exception("API failure request_id=%s", g.request_id)
        return error(500, "服务暂时无法处理请求，请凭请求编号检查日志")

    legacy_size_error = base.request_entity_too_large

    @app.errorhandler(413)
    def payload_error(exc):
        if request.path.startswith('/api/v1/'):
            return error(413, "请求内容超过 2 MiB 上限")
        return legacy_size_error(exc)

    def runtime_data():
        value = server.runtime_client.snapshot()
        if value.get("schema_version") != 1 or value.get("error"):
            return None, (value.get("error", "运行采集器不可用"), value.get("error_code", "UNAVAILABLE"))
        try:
            age = server.runtime_client.sample_age(value)
        except ValueError as exc:
            return None, (str(exc), "STALE_DATA")
        return {**value, "freshness": {"age_seconds": round(age, 3),
                    "max_age_seconds": server.runtime_client.MAX_SAMPLE_AGE}}, None

    @api.get("/instance")
    def instance():
        return jsonify({"data": {"id": instance_identity(server.STATE_DIR),
                                 "name": os.environ.get("SUITE_INSTANCE_NAME", "LovestDNS"),
                                 "kind": "paopaodns", "edition": "integrated", "version": server.VERSION},
                        "meta": meta()})

    @api.get("/capabilities")
    def capabilities():
        value = app.make_response(server.capabilities()).get_json()
        observer = g.api_role == "observer"
        value.update({"instance_id": instance_identity(server.STATE_DIR), "api_version": "1",
                      "name": os.environ.get("SUITE_INSTANCE_NAME", "LovestDNS"),
                      "access_role": g.api_role, "record_types": list(server.RECORD_TYPES),
                      "permissions": {"observe": True, "diagnose": True,
                                      "configure": not observer, "maintain": not observer}})
        if observer:
            value["features"] = {**value["features"], "config_history": False,
                                 "config_apply": False, "redis_snapshot": False, "restart_dns_core": False,
                                 "conditional_config_write": False, "config_state": False, "query_recording": False}
        return jsonify({"data": value, "meta": meta()})

    @api.get("/runtime")
    def runtime():
        value, problem = runtime_data()
        return error(503, *problem) if problem else jsonify({"data": value, "meta": meta()})

    @api.get("/metrics")
    def metrics():
        value, problem = runtime_data()
        return error(503, *problem) if problem else jsonify({"data": {**metric_values(value), "freshness": value["freshness"]}, "meta": meta()})

    for path, method, operation, handler, _ in OPERATIONS:
        def delegated(handler=handler, **kwargs):
            view = getattr(server, handler, None) or getattr(base, handler)
            return invoke(view, **kwargs)
        api.add_url_rule(path, operation, delegated, methods=[method])

    @api.get("/openapi.json", endpoint="schema")
    def schema():
        # OpenAPI itself is returned directly, so standard tooling can consume it.
        from api_schema import openapi_document
        return jsonify(openapi_document(server.VERSION))

    from gateway_integration import register_routes
    register_routes(api, server, meta)
    app.register_blueprint(api)
