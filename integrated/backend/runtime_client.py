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
    if operation not in {"capabilities", "plan", "tasks", "task", "submit", "config_state"}:
        raise ValueError("Unsupported manager request")
    payload = {"operation": operation}
    if operation in {"submit", "task"}:
        payload["task"] = task
    path = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite")) / "manager.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(path))
            sock.sendall(json.dumps(payload).encode() + b"\n")
            with sock.makefile("rb") as stream:
                raw = stream.readline(4 * 1024 * 1024 + 1)
        if not raw.endswith(b"\n") or len(raw) > 4 * 1024 * 1024:
            raise ValueError("Invalid manager response")
        response = json.loads(raw)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("Invalid manager response")
        if not response["ok"]:
            if response.get("code") == "UNAVAILABLE":
                raise RuntimeError(str(response.get("error", "维护服务暂不可用"))[:500])
            raise ValueError(str(response.get("error", "维护请求被拒绝"))[:500])
        return response["result"]
    except OSError as exc:
        raise RuntimeError("核心维护服务暂不可用") from exc


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
