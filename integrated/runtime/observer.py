"""One read-only collector per container; no reset, flush, or shell commands."""
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import shutil
import threading
import time
import xmlrpc.client
from runtime.dns_listeners import summary as dns_listener_summary

RUN_DIR = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
MAX_RESPONSE = 512 * 1024
INTERVAL = 5
STAT_KEYS = {"total.num.queries", "total.num.cachehits", "total.num.cachemiss",
             "total.num.prefetch", "total.num.expired", "total.requestlist.exceeded",
             "total.requestlist.current.all", "total.requestlist.current.user", "time.up",
             "mem.cache.rrset", "mem.cache.message", "mem.mod.iterator"}
REDIS_KEYS = {"used_memory", "maxmemory", "evicted_keys", "keyspace_hits", "keyspace_misses",
              "loading", "rdb_last_save_time", "rdb_changes_since_last_save",
              "rdb_bgsave_in_progress", "rdb_last_bgsave_status", "redis_version"}


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(str(RUN_DIR / "supervisor.sock"))


class UnixTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        return UnixHTTPConnection(host)


def process_status():
    with xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=UnixTransport()) as rpc:
        return {p["name"]: {k: p[k] for k in ("statename", "pid", "start", "exitstatus")}
                for p in rpc.supervisor.getAllProcessInfo()}


def exchange_unix(path, payload):
    deadline = time.monotonic() + 2
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        sock.sendall(payload)
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Statistics read timed out")
            sock.settimeout(remaining)
            chunk = sock.recv(8192)
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_RESPONSE:
                raise ValueError("Statistics response exceeds limit")


def parse_unbound(text):
    result = {}
    for line in text.splitlines():
        key, sep, raw = line.partition("=")
        if sep and key in STAT_KEYS:
            value = float(raw)
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid Unbound counter")
            result[key] = value
    if "total.num.queries" not in result or "time.up" not in result:
        raise ValueError("Unbound statistics incomplete")
    return result


def unbound_stats(path):
    return parse_unbound(exchange_unix(path, b"UBCT1 stats_noreset\n").decode("ascii"))


def parse_redis(text):
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key in REDIS_KEYS:
            if value.isdigit():
                result[key] = int(value)
            elif key in {"redis_version", "rdb_last_bgsave_status"}:
                result[key] = value[:80]
        elif key in {"db0", "db1"}:
            result[key] = {k: int(v) for k, v in re.findall(r"(keys|expires|avg_ttl)=(\d+)", value)}
    if "loading" not in result or "used_memory" not in result:
        raise ValueError("Redis statistics incomplete")
    return result


def redis_stats(path="/tmp/redis.sock"):
    # Read one RESP bulk string; INFO is read-only and must not reset counters.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(path))
        sock.sendall(b"*2\r\n$4\r\nINFO\r\n$3\r\nALL\r\n")
        with sock.makefile("rb") as stream:
            header = stream.readline(80)
            if not re.fullmatch(rb"\$\d+\r\n", header):
                raise ValueError("Invalid Redis INFO response")
            size = int(header[1:-2])
            if size > MAX_RESPONSE:
                raise ValueError("Redis INFO exceeds limit")
            data = stream.read(size)
            if len(data) != size or stream.read(2) != b"\r\n":
                raise ValueError("Incomplete Redis INFO response")
    return parse_redis(data.decode("utf-8"))


def rate(previous, current, elapsed, same_process):
    def unavailable(reason):
        return {"comparable": False, "qps": None, "cache_hit_ratio": None,
                "comparison_reason": reason, "window_seconds": None}
    if not previous:
        return unavailable("first_sample")
    if not math.isfinite(elapsed) or elapsed <= 0:
        return unavailable("clock_changed")
    if not same_process:
        return unavailable("process_changed")
    keys = ("total.num.queries", "total.num.cachehits", "total.num.cachemiss", "time.up")
    if any(k not in previous or k not in current for k in keys):
        return unavailable("missing_counters")
    if any(current[k] < previous[k] for k in keys):
        return unavailable("counters_reset")
    queries, hits, misses = (current[k] - previous[k] for k in keys[:3])
    return {"comparable": True, "qps": round(queries / elapsed, 2),
            "comparison_reason": "comparable", "window_seconds": round(elapsed, 3),
            "cache_hit_ratio": round(hits / (hits + misses), 4) if hits + misses else None}


def read_boot_env():
    try:
        return json.loads((RUN_DIR / "core-env.json").read_text())
    except (OSError, ValueError):
        return {}


def collect(previous=None):
    now = time.time()
    sampled_monotonic = time.monotonic()
    env = read_boot_env()
    states = process_status()
    required = ["redis", "unbound-raw", "watcher"]
    if env.get("CNAUTO") != "no":
        required += ["mosdns", "dnscrypt", "unbound-forward"]
        if re.search(r":\d+$", env.get("SOCKS5", "")):
            required.append("dnscrypt-socks")
    ready = (RUN_DIR / "ready").exists()
    dns = dns_listener_summary(env, states)
    # A restart can change the supervisor-to-PID mapping during /proc reads.
    # Discard only listener evidence if that mapping cannot be reconfirmed.
    try:
        confirmed_states = process_status()
    except (OSError, ValueError, xmlrpc.client.Error):
        confirmed_states = {}
    for name, observation in dns['listeners'].items():
        if observation['available'] and states.get(name) != confirmed_states.get(name):
            dns['listeners'][name] = {'available': False, 'reason': 'unconfirmed', 'endpoints': []}
    sample = {"schema_version": 1, "sampled_at": now, "interval_seconds": INTERVAL,
              "ready": ready, "core_healthy": ready and all(
                  states.get(name, {}).get("statename") == "RUNNING" for name in required),
              "components": states, "layers": {}, "dns": dns}
    sample['_monotonic_at'] = sampled_monotonic
    for layer, component, path in [("recursive", "unbound-raw", "/tmp/uc_raw.sock"),
                                    ("forward", "unbound-forward", "/tmp/uc_forward.sock")]:
        if states.get(component, {}).get("statename") != "RUNNING":
            sample["layers"][layer] = {"available": False, "error": "组件未运行"}
            continue
        try:
            counters = unbound_stats(path)
            old = (previous or {}).get("layers", {}).get(layer, {})
            old_process = (previous or {}).get("components", {}).get(component)
            sample["layers"][layer] = {"available": True, "counters": counters,
                **rate(old.get("counters"), counters, sampled_monotonic - (previous or {}).get("_monotonic_at", sampled_monotonic),
                       old_process == states[component])}
        except (OSError, ValueError) as exc:
            sample["layers"][layer] = {"available": False, "error": str(exc)[:200]}
    try:
        sample["redis"] = {"available": True, "info": redis_stats()}
    except (OSError, ValueError) as exc:
        sample["redis"] = {"available": False, "error": str(exc)[:200]}
    sample["persistence"] = persistence_status(sample["redis"], Path("/data"))
    return sample


def persistence_status(redis, data_dir):
    info = redis.get("info", {}) if redis.get("available") else {}
    path = Path(data_dir) / "redis_dns_v2.rdb"
    result = {"scope": "Redis RDB，不包含其他组件的内存缓存或查询历史",
              "last_save_time": info.get("rdb_last_save_time"),
              "last_save_status": info.get("rdb_last_bgsave_status"),
              "saving": info.get("rdb_bgsave_in_progress"),
              "loading": info.get("loading"), "changes_since_save": info.get("rdb_changes_since_last_save")}
    try:
        if path.is_symlink():
            raise ValueError("默认 RDB 路径是链接，未读取")
        result["rdb_bytes"] = path.stat().st_size if path.is_file() else None
        result["disk_free_bytes"] = shutil.disk_usage(data_dir).free
    except (OSError, ValueError) as exc:
        result["storage_error"] = str(exc)[:160]
    warnings = []
    if info.get("rdb_last_bgsave_status") == "err":
        warnings.append("最近一次后台保存失败")
    if result.get("rdb_bytes") and result.get("disk_free_bytes", 0) < result["rdb_bytes"] * 2:
        warnings.append("磁盘余量不足两份当前 RDB，保存或备份可能失败")
    result["warnings"] = warnings
    return result


class Collector:
    def __init__(self):
        self.snapshot = None
        self.lock = threading.Lock()

    def run(self):
        previous = None
        deadline = time.monotonic()
        while True:
            try:
                sample = collect(previous)
                previous = sample
            except Exception as exc:
                sample = {"schema_version": 1, "sampled_at": time.time(), "error": str(exc)[:200]}
                previous = None
            with self.lock:
                self.snapshot = {key: value for key, value in sample.items() if not key.startswith('_')}
            deadline += INTERVAL
            now = time.monotonic()
            if deadline < now:
                deadline = now + INTERVAL  # Skip missed rounds, never catch up in a burst.
            time.sleep(max(0, deadline - now))


def main():
    collector = Collector()
    threading.Thread(target=collector.run, daemon=True).start()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            if self.rfile.readline(32) != b"snapshot\n":
                self.wfile.write(b'{"error":"Unsupported operation"}\n')
                return
            with collector.lock:
                payload = collector.snapshot or {"error": "Collector starting"}
            self.wfile.write(json.dumps(payload, allow_nan=False).encode() + b"\n")

    socket_path = RUN_DIR / "observer.sock"
    socket_path.unlink(missing_ok=True)
    # Sequential local requests are bounded; collector is independent of web workers.
    server = socketserver.UnixStreamServer(str(socket_path), Handler)
    os.chmod(socket_path, 0o600)
    server.serve_forever()


if __name__ == "__main__":
    main()
