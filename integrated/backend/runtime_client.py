"""Read-only protocol to the local collector; never forwards user commands."""
import json
import math
import os
from pathlib import Path
import socket
import time

MAX_SAMPLE_AGE = 25


def sample_age(value, now=None):
    sampled = value.get("sampled_at")
    if isinstance(sampled, bool) or not isinstance(sampled, (int, float)) or not math.isfinite(sampled):
        raise ValueError("Invalid collector timestamp")
    age = (time.time() if now is None else now) - sampled
    if age < 0 or age > MAX_SAMPLE_AGE:
        raise ValueError("Collector sample is stale or clock is inconsistent")
    return age


def manager_request(operation, task=None):
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("本平台没有本地 Unix socket，维护服务不可用")
    if operation not in {"capabilities", "plan", "tasks", "task", "submit", "config_state", "filter_status", "filter_action", "filter_check"}:
        raise ValueError("Unsupported manager request")
    payload = {"operation": operation}
    if operation in {"submit", "task", "filter_action", "filter_check"}:
        payload["task"] = task
    path = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite")) / "manager.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(path))
            sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            with sock.makefile("rb") as stream:
                raw = stream.readline(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeError("核心维护服务暂不可用") from exc
    # An incomplete transport reply can arrive after the task was committed.
    # Only a valid conflict response proves that the manager rejected submission.
    message = "维护服务响应无效，请核对任务状态"
    if not raw.endswith(b"\n") or len(raw) > 4 * 1024 * 1024:
        raise RuntimeError(message)
    try:
        response = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(message) from exc
    if isinstance(response, dict):
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            return response["result"]
        if response.get("ok") is False:
            if response.get("code") == "CONFLICT":
                raise ValueError(str(response.get("error", "维护请求被拒绝"))[:500])
            if response.get("code") == "UNAVAILABLE":
                raise RuntimeError(str(response.get("error", "维护服务暂不可用"))[:500])
    raise RuntimeError(message)


def snapshot():
    if not hasattr(socket, "AF_UNIX"):
        return {"available": False, "error": "Local Unix sockets unavailable on this platform"}
    path = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite")) / "observer.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(path))
            sock.sendall(b"snapshot\n")
            with sock.makefile("rb") as stream:
                data = stream.readline(512 * 1024 + 1)
                if len(data) > 512 * 1024 or not data.endswith(b"\n"):
                    raise ValueError("Invalid collector response size")
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("error"):
            raise ValueError("Collector unavailable")
        try:
            sample_age(value)
        except ValueError as exc:
            return {"available": False, "error": str(exc)[:200], "error_code": "STALE_DATA"}
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"available": False, "error": str(exc)[:200]}


def query_records_request(payload):
    from runtime.query_recorder import validate_request
    payload = validate_request(dict(payload))
    path = Path(os.environ.get('SUITE_RUN_DIR', '/run/paopaodns-suite')) / 'query-recorder.sock'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(4)
            sock.connect(str(path))
            sock.sendall(json.dumps(payload).encode() + b'\n')
            with sock.makefile('rb') as stream:
                raw = stream.readline(512 * 1024 + 1)
        if len(raw) > 512 * 1024 or not raw.endswith(b'\n'):
            raise RuntimeError('查询记录响应超过上限')
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('ok') is not True:
            raise RuntimeError('查询记录服务暂不可用')
        return value['result']
    except (OSError, AttributeError, ValueError, KeyError) as exc:
        raise RuntimeError('查询记录服务暂不可用') from exc
