"""Integrated-only API overlay. The frozen standalone backend is imported unchanged."""
import json
import os
import re
from pathlib import Path
import sqlite3
import shutil

from flask import jsonify, request, g
from werkzeug.exceptions import Conflict, ServiceUnavailable
import app as base
from dns_wire import RECORD_TYPES, TYPE_NAMES, parse_response
from history import History
import runtime_client
from config_versions import revision as file_revision, expected_revision, check_revision
from runtime.locking import MaintenanceBusy, maintenance_lock
from runtime.tasks import validate_submission
from runtime.admission import CapacityBusy, slots
from installation import read_object, recorded, installation_summary, provenance_summary

app = base.app
if os.environ.get("SUITE_DIST_DIR"):
    app.static_folder = os.environ["SUITE_DIST_DIR"]
VERSION = "0.1.0-dev.lovestdns-native.20261112.query1"
STATE_DIR = Path(os.environ.get("SUITE_STATE_DIR", "/var/lib/paopaodns-suite"))
RUN_DIR = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
history = History(STATE_DIR)


class SaveUnconfirmed(ServiceUnavailable):
    api_code = "SAVE_UNCONFIRMED"

base.DNS_TEST_SERVER = os.environ.get("DNS_TEST_SERVER", "127.0.0.1")
base.DNS_RECORD_TYPES = RECORD_TYPES
base.DNS_TYPE_NAMES = TYPE_NAMES
base._parse_dns_response = lambda packet, query_id, domain=None, record_type=None: parse_response(
    packet, query_id, domain, record_type, base._read_dns_name)
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


def _original_write(filename, content):
    """Write exact UTF-8 bytes while retaining the upstream inotify contract."""
    path = base._safe_path(filename)
    if path is None:
        return False, "File not allowed"
    payload = content.encode("utf-8")
    if len(payload) > base.FILE_SIZE_LIMITS.get(filename, base.FILE_SIZE_LIMITS["_default"]):
        return False, "Content exceeds size limit"
    before = _configuration_bytes(filename, path)
    try:
        with base.WRITE_LOCKS[filename]:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            if before is not None:
                try:
                    shutil.copyfile(path, str(path) + ".bak")
                except OSError:
                    pass  # The durable intent is the authoritative backup.
            try:
                with open(path, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
            except OSError:
                _restore_failed_write(path, before is not None, before or b"")
                raise
        return True, ""
    except OSError as exc:
        return False, str(exc)


def historized_write(filename, content):
    try:
        with maintenance_lock(STATE_DIR):
            return _write_with_history(filename, content)
    except MaintenanceBusy as exc:
        raise Conflict(str(exc)) from exc


def _write_with_history(filename, content):
    prepared = False
    try:
        path = base._safe_path(filename)
        if path is None:
            return False, "File not allowed"
        before = _configuration_bytes(filename, path)
        history.complete_write(filename, before)
        check_revision(expected_revision(), before)
        history.prepare_write(filename, before, content.encode("utf-8"))
        prepared = True
        success, error = _original_write(filename, content)
        current = _configuration_bytes(filename, path)
        history.complete_write(filename, current)
        if (success and current != content.encode('utf-8')) or (not success and current != before):
            raise SaveUnconfirmed("保存结果待核对：磁盘内容发生变化，候选文件和原版本已保留在历史中，请重新读取并合并")
        if success:
            g.saved_revision = file_revision(current)
        return success, error
    except (OSError, ValueError, sqlite3.Error) as exc:
        if prepared:
            raise SaveUnconfirmed("保存结果待核对：文件可能已写入，恢复记录已保留。请重新读取文件和历史后再决定是否重试") from exc
        return False, f"Configuration history unavailable: {exc}"


def _configuration_bytes(filename, path):
    if not Path(path).exists():
        return None
    limit = base.FILE_SIZE_LIMITS.get(filename, base.FILE_SIZE_LIMITS["_default"])
    with Path(path).open("rb") as source:
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Existing configuration exceeds history size limit")
    return content


def _restore_failed_write(path, existed_before, original_bytes, read_ok=True):
    # The frozen writer's truthiness check misses a valid empty original file.
    if existed_before and read_ok:
        try:
            with open(path, "wb") as target:
                target.write(original_bytes)
                target.flush()
                os.fsync(target.fileno())
            return
        except OSError:
            pass
    _original_restore_failed_write(path, existed_before, original_bytes, read_ok)


_original_restore_failed_write = base._restore_failed_write
base._restore_failed_write = _restore_failed_write


def _limited_diagnostic(view):
    # Both legacy and v1 routes use the same cross-worker admission boundary.
    def limited(*args, **kwargs):
        try:
            with slots(RUN_DIR / 'diagnostic-slots', 2):
                return view(*args, **kwargs)
        except CapacityBusy as exc:
            response = jsonify({'error': str(exc)})
            response.status_code = 503
            response.headers['Retry-After'] = '1'
            return response
    return limited


for _name in ('api_dns_test', 'api_dns_compare', 'api_health_check'):
    _view = _limited_diagnostic(getattr(base, _name))
    setattr(base, _name, _view)
    app.view_functions[_name] = _view


@app.before_request
def recover_configuration_history():
    # Lazy recovery also works for prefork workers and after a process crash.
    # The mutation path does this under its own lock immediately before writing.
    filename = (request.view_args or {}).get("filename")
    if request.method == "GET" and filename in base.ALLOWED_FILES:
        path = base._safe_path(filename)
        if path is not None:
            try:
                if history.has_pending(filename):
                    with maintenance_lock(STATE_DIR):
                        history.complete_write(filename, _configuration_bytes(filename, path))
            except MaintenanceBusy as exc:
                raise Conflict(str(exc)) from exc
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ServiceUnavailable("配置恢复记录暂不可用，请稍后重试") from exc


base._atomic_write = historized_write


def boot_env():
    try:
        values = json.loads((RUN_DIR / "core-env.json").read_text())
        visible = base.RUNTIME_ENV_KEYS | {"DNSPORT", "DNS_SERVERNAME", "UPDATE", "SERVER_IP", "SOCKS5",
            "EXPIRED_FLUSH", "CUSTOM_FORWARD_TTL", "AUTO_FORWARD", "AUTO_FORWARD_CHECK", "USE_HOSTS",
            "HTTP_FILE", "SHUFFLE", "ADDINFO", "SAFEMODE", "QUERY_TIME"}
        return {key: values[key] for key in visible if key in values and isinstance(values[key], str)}
    except (OSError, ValueError):
        return {}


def reload_info(filename, env=None):
    env = boot_env()
    if filename == "unbound_custom.conf":
        return {"auto_reload": False, "requires_reload": False, "requires_restart": True,
                "watched_now": False, "condition": "需重启 DNS 服务重新生成 Unbound 配置", "verified": False}
    if filename == "custom_mod.yaml":
        return {"auto_reload": False, "requires_reload": True, "requires_restart": False,
                "watched_now": False, "condition": "保存后请到配置中心预览并应用，再查询目标域名验证规则", "verified": False}
    if filename == "custom_env.ini":
        return {"auto_reload": False, "requires_reload": False, "requires_restart": True,
                "watched_now": False, "condition": "保存后请到配置中心重启 DNS 核心，重新生成初始化配置", "verified": False}
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
base._runtime_env = boot_env


def build_info():
    info = read_object(os.environ.get("SUITE_BUILD_INFO", "/opt/suite/build-info.json"))
    return {**info, **{key: recorded(info.get(key)) or 'unknown' for key in ('source_commit', 'image')}}


@app.get("/api/capabilities")
def capabilities():
    current = runtime_client.snapshot()
    available = current.get("schema_version") == 1
    env = boot_env()
    raw_port = os.environ.get("SUITE_DNS_PORT") or (env.get("DNSPORT", "53") if env.get("CNAUTO") == "no" else "53")
    port = int(raw_port) if raw_port.isdigit() and 0 < int(raw_port) < 65536 else 53
    try:
        management = runtime_client.manager_request("capabilities")
    except (RuntimeError, ValueError):
        management = {}
    info = build_info()
    return jsonify({"edition": "integrated", "version": VERSION, "core": info,
        "installation": installation_summary(), "provenance": provenance_summary(info, app.static_folder),
        "deployment": "native" if os.environ.get("LOVESTDNS_SYSTEMD") == "1" else "container",
        "dns_defaults": {"server": base.DNS_TEST_SERVER, "port": port},
        "maintenance": management.get('worker', {'available': bool(management), 'phase': 'ready' if management else 'unavailable'}),
        "features": {"dns_debug": True, "config_history": True,
                     "runtime_snapshot": available, "config_apply": management.get("apply_custom_mod") is True,
                     "redis_snapshot": management.get("save_redis_snapshot") is True,
                     "restart_dns_core": management.get("restart_dns_core") is True,
                     "conditional_config_write": True, "config_state": True,
                     "query_recording": True, "cache_flush": False, "client_policies": False},
        "runtime_error": current.get("error")})


def _query_records(payload):
    try:
        value = runtime_client.query_records_request(payload)
        if payload['operation'] == 'recent':
            from api_v1 import instance_identity
            value = {**value, 'instance_id': instance_identity(STATE_DIR)}
        return jsonify(value)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503


def query_recording_status():
    return _query_records({'operation': 'status'})


def query_recording_configure():
    body = request.get_json()
    if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
        return jsonify({'error': '只接受 enabled 布尔值'}), 400
    return _query_records({'operation': 'configure', **body})


def query_records_clear():
    return _query_records({'operation': 'clear'})


def query_records_recent():
    if set(request.args) - {'limit', 'before', 'source', 'domain', 'qtype'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        return jsonify({'error': '查询筛选参数无效'}), 400
    payload = {'operation': 'recent', **request.args.to_dict()}
    try:
        for key in ('limit', 'before', 'qtype'):
            if key in payload:
                if not re.fullmatch(r'[0-9]{1,19}', payload[key]):
                    raise ValueError('数值筛选参数无效')
                payload[key] = int(payload[key])
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return _query_records(payload)


def api_read_file(filename):
    path = base._safe_path(filename)
    if path is None:
        return jsonify({'error': 'File not allowed'}), 403
    try:
        with maintenance_lock(STATE_DIR):
            target = Path(path)
            if not target.exists():
                return jsonify({'error': 'File not found', 'revision': 'missing'}), 404
            limit = base.FILE_SIZE_LIMITS.get(filename, base.FILE_SIZE_LIMITS['_default'])
            if target.stat().st_size > limit:
                return jsonify({'error': 'File too large'}), 413
            data = target.read_bytes()
            if len(data) > limit:
                return jsonify({'error': 'File too large'}), 413
            response = jsonify({'filename': filename, 'content': data.decode('utf-8', errors='replace').replace('\r\n', '\n'),
                                'revision': file_revision(data), **reload_info(filename)})
            response.set_etag(file_revision(data))
            return response
    except MaintenanceBusy as exc:
        return jsonify({'error': str(exc)}), 409
    except OSError:
        return jsonify({'error': 'Failed to read file'}), 503


@app.get('/api/config/state')
def configuration_state():
    try:
        return jsonify(runtime_client.manager_request('config_state'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 503


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
    if component not in {"mosdns", "unbound-raw", "unbound-forward", "redis", "dnscrypt", "dnscrypt-socks", "watcher", "bootstrap", "manager", "observer", "query-recorder", "cron", "updater"}:
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
        sensitive = dict(os.environ)
        for filename in ("core-env.json", "mosdns-env.json"):
            try:
                sensitive.update(json.loads((RUN_DIR / filename).read_text()))
            except (OSError, ValueError):
                pass
        for key, value in sensitive.items():
            if isinstance(value, str) and len(value) >= 4 and base.SENSITIVE_KEY_PATTERNS.search(key):
                data = data.replace(value, "***")
        return jsonify({"component": component, "lines": data.splitlines()[-200:]})
    except OSError:
        return jsonify({"component": component, "lines": [], "error": "日志尚不可用"})


@app.after_request
def saved_file_revision(response):
    if response.status_code < 300 and hasattr(g, 'saved_revision'):
        response.set_etag(g.saved_revision)
        body = response.get_json(silent=True)
        if isinstance(body, dict):
            target = body.get('data', body)
            target['revision'] = g.saved_revision
            response.set_data(app.json.dumps(body))
    return response


# Keep standalone sources frozen; the versioned API uses the same handlers.
import sys
from api_v1 import register
register(sys.modules[__name__])
from browser_auth import register as register_browser_auth
register_browser_auth(sys.modules[__name__])
