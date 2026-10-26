"""Integrated-only API overlay. The frozen standalone backend is imported unchanged."""
import json
import os
from pathlib import Path
import sqlite3

from flask import jsonify, request
import app as base
from dns_wire import RECORD_TYPES, TYPE_NAMES, parse_response
from history import History
import runtime_client
from runtime.locking import MaintenanceBusy, maintenance_lock
from runtime.tasks import validate_submission

app = base.app
if os.environ.get("SUITE_DIST_DIR"):
    app.static_folder = os.environ["SUITE_DIST_DIR"]
VERSION = "0.1.0-dev.dns.20261026.1"
STATE_DIR = Path(os.environ.get("SUITE_STATE_DIR", "/var/lib/paopaodns-suite"))
RUN_DIR = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
history = History(STATE_DIR)
base.DNS_TEST_SERVER = "127.0.0.1"
base.DNS_RECORD_TYPES = RECORD_TYPES
base.DNS_TYPE_NAMES = TYPE_NAMES
base._parse_dns_response = lambda packet, query_id, domain=None, record_type=None: parse_response(
    packet, query_id, domain, record_type, base._read_dns_name)
_original_write = base._atomic_write
_original_path = base._safe_path


def safe_path(filename):
    path = _original_path(filename)
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_symlink() or candidate.resolve().parent != Path(base.DATA_DIR).resolve():
        return None
    return path


base._safe_path = safe_path


def historized_write(filename, content):
    try:
        with maintenance_lock(STATE_DIR):
            return _write_with_history(filename, content)
    except MaintenanceBusy as exc:
        return False, str(exc)


def _write_with_history(filename, content):
    try:
        with history.write_lock() as db:
            path = base._safe_path(filename)
            if path is None or Path(path).is_symlink():
                return False, "File not allowed"
            if Path(path).exists():
                if Path(path).stat().st_size > base.FILE_SIZE_LIMITS.get(filename, base.FILE_SIZE_LIMITS["_default"]):
                    return False, "Existing configuration exceeds history size limit"
                history.record(db, filename, Path(path).read_bytes(), "before_save")
            success, error = _original_write(filename, content)
            if success:
                history.record(db, filename, content.encode("utf-8"), "saved")
                history.prune(db)
            return success, error
    except (OSError, ValueError, sqlite3.Error) as exc:
        return False, f"Configuration history unavailable: {exc}"


base._atomic_write = historized_write


def boot_env():
    try:
        values = json.loads((RUN_DIR / "core-env.json").read_text())
        return {key: values[key] for key in base.RUNTIME_ENV_KEYS | {"DNSPORT"} if key in values}
    except (OSError, ValueError):
        return {}


def reload_info(filename, env=None):
    env = boot_env()
    if filename == "unbound_custom.conf":
        return {"auto_reload": False, "requires_reload": False, "requires_restart": True,
                "watched_now": False, "condition": "需重启集成容器重新生成 Unbound 配置", "verified": False}
    if filename == "custom_mod.yaml":
        return {"auto_reload": False, "requires_reload": True, "requires_restart": False,
                "watched_now": False, "condition": "保存后请到配置历史与应用预览并应用，再查询目标域名验证规则", "verified": False}
    enabled = env.get("CNAUTO") != "no" and bool(env)
    conditions = base.CONDITIONAL_AUTO_RELOAD.get(filename, {})
    for key, expected in conditions.items():
        if key == "_needs_custom_forward":
            enabled &= bool(base.re.search(r":\d+$", env.get("CUSTOM_FORWARD", "")))
        elif key == "_needs_rules_ttl":
            enabled &= env.get("RULES_TTL", "0").isdigit() and int(env.get("RULES_TTL", "0")) > 0
        else:
            enabled &= env.get(key) == expected
    note = "根据核心启动配置判断监听条件；不代表配置已加载"
    if not enabled:
        note = "核心未准备好或启动配置不满足监听条件；请检查后重启"
    if filename == "custom_env.ini":
        note += "；初始化变量需重启重新生成模板"
    return {"auto_reload": True, "requires_reload": False, "requires_restart": False,
            "watched_now": bool(enabled), "condition": note, "verified": False}


base._file_reload_info = reload_info


def build_info():
    try:
        return json.loads(Path(os.environ.get("SUITE_BUILD_INFO", "/opt/suite/build-info.json")).read_text())
    except (OSError, ValueError):
        return {"source_commit": "unknown", "image": "unknown"}


@app.get("/api/capabilities")
def capabilities():
    current = runtime_client.snapshot()
    available = current.get("schema_version") == 1
    env = boot_env()
    raw_port = env.get("DNSPORT", "53") if env.get("CNAUTO") == "no" else "53"
    port = int(raw_port) if raw_port.isdigit() and 0 < int(raw_port) < 65536 else 53
    try:
        management = runtime_client.manager_request("capabilities")
    except (RuntimeError, ValueError):
        management = {}
    return jsonify({"edition": "integrated", "version": VERSION, "core": build_info(),
        "dns_defaults": {"server": "127.0.0.1", "port": port},
        "features": {"dns_debug": True, "config_history": True,
                     "runtime_snapshot": available, "config_apply": management.get("apply_custom_mod") is True,
                     "redis_snapshot": management.get("save_redis_snapshot") is True,
                     "cache_flush": False, "client_policies": False},
        "runtime_error": current.get("error")})


@app.get("/api/config/apply-plan")
def apply_plan():
    try:
        return jsonify(runtime_client.manager_request("plan"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/maintenance/tasks")
def maintenance_tasks():
    try:
        return jsonify(runtime_client.manager_request("tasks"))
    except (RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 503


def maintenance_task(task_id):
    if not base.re.fullmatch(r"[a-f0-9]{32}", task_id):
        return jsonify({"error": "任务编号无效"}), 400
    try:
        result = runtime_client.manager_request("task", task_id)
        if not result.get("task"):
            return jsonify({"error": "任务不存在或已超过保留数量"}), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503


@app.post("/api/maintenance/tasks")
def submit_task():
    body = request.get_json(silent=True)
    try:
        validate_submission(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        return jsonify(runtime_client.manager_request("submit", body)), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/runtime")
def runtime():
    current = runtime_client.snapshot()
    return jsonify(current), 200 if current.get("schema_version") == 1 else 503


@app.get("/api/history/<filename>")
def list_history(filename):
    if filename not in base.ALLOWED_FILES:
        return jsonify({"error": "File not allowed"}), 403
    try:
        return jsonify({"filename": filename, "revisions": history.list(filename)})
    except (OSError, ValueError, sqlite3.Error):
        return jsonify({"error": "Configuration history unavailable"}), 503


@app.get("/api/history/<filename>/<int:revision>")
def read_history(filename, revision):
    if filename not in base.ALLOWED_FILES:
        return jsonify({"error": "File not allowed"}), 403
    try:
        content = history.read(filename, revision)
    except (OSError, ValueError, UnicodeError, sqlite3.Error):
        return jsonify({"error": "Configuration history unavailable"}), 503
    if content is None:
        return jsonify({"error": "Revision not found"}), 404
    return jsonify({"filename": filename, "revision": revision, "content": content})


@app.post("/api/history/<filename>/<int:revision>/restore")
def restore_history(filename, revision):
    if filename not in base.ALLOWED_FILES:
        return jsonify({"error": "File not allowed"}), 403
    try:
        content = history.read(filename, revision)
    except (OSError, ValueError, UnicodeError, sqlite3.Error):
        return jsonify({"error": "Configuration history unavailable"}), 503
    if content is None:
        return jsonify({"error": "Revision not found"}), 404
    size_error = base._content_size_error(filename, content)
    if size_error:
        return jsonify({"error": size_error}), 413
    valid, error = base._validate_file_content(filename, content)
    if not valid:
        return jsonify({"error": error}), 400
    success, error = historized_write(filename, content)
    if not success:
        return jsonify({"error": error}), 500
    return jsonify({"message": "已恢复文件；请根据生效条件应用并验证", "filename": filename,
                    **reload_info(filename)})


@app.get("/api/core-logs/<component>")
def core_logs(component):
    if component not in {"mosdns", "unbound-raw", "unbound-forward", "redis", "dnscrypt", "dnscrypt-socks", "watcher"}:
        return jsonify({"error": "Unknown component"}), 404
    try:
        with (STATE_DIR / "logs" / f"{component}.log").open("rb") as source:
            source.seek(0, 2)
            start = max(0, source.tell() - 64 * 1024)
            source.seek(start)
            data = source.read(64 * 1024).decode("utf-8", errors="replace")
            if start:
                data = data.partition("\n")[2]
        # Upstream logs may include environment diagnostics in custom deployments.
        for key, value in os.environ.items():
            if value and len(value) >= 4 and base.SENSITIVE_KEY_PATTERNS.search(key):
                data = data.replace(value, "***")
        return jsonify({"component": component, "lines": data.splitlines()[-200:]})
    except OSError:
        return jsonify({"component": component, "lines": [], "error": "日志尚不可用"})


# Keep standalone sources frozen; the versioned API uses the same handlers.
import sys
from api_v1 import register
register(sys.modules[__name__])
