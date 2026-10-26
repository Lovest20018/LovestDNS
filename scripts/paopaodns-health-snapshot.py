#!/usr/bin/env python3
"""Read-only PaoPaoDNS snapshot for its Linux Docker host (Python 3, Docker).
Print JSON to stdout or use --output FILE. Does not restart, reconfigure,
flush caches, install packages, or create scheduled tasks. Normal test DNS
queries may warm cache entries. Runtime timeout defaults to 6s per query.
Exit codes: 0=all probes and metadata collected, 1=cannot inspect container,
2=partial report (inspect JSON for DNS failures or unavailable metadata).
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time

def run(command, timeout=10):
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"exit": None, "stdout": "", "stderr": str(e)}

def control(root, command, control_socket):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(4)
        s.connect(root + control_socket)
        s.sendall(("UBCT1 " + command + "\n").encode())
        blocks = []
        deadline = time.monotonic() + 4
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Unbound statistics read timed out")
            s.settimeout(remaining)
            data = s.recv(65536)
            if not data:
                break
            blocks.append(data)
            size += len(data)
            if size > 1024 * 1024:
                raise ValueError("Unbound statistics response too large")
        return b"".join(blocks).decode()

def stats(root, control_socket):
    try:
        out = control(root, "stats_noreset", control_socket)
        values = {
            k: float(v)
            for k, v in (line.split("=", 1) for line in out.splitlines() if "=" in line)
            if k.startswith("total.") or k == "time.up"
        }
        if not values or not all(math.isfinite(value) for value in values.values()):
            return {"error": "Unbound statistics missing or invalid"}
        return values
    except (OSError, ValueError) as e:
        return {"error": str(e)}

def query(container, domain, role, port, timeout, tcp=False):
    command = ["docker", "exec", container, "dig", "@127.0.0.1", "-p", str(port),
               domain, "A", "+time=" + str(timeout), "+tries=1",
               "+noall", "+answer", "+comments", "+stats"]
    if tcp:
        command.append("+tcp")
    result = run(command, timeout + 3)
    status = re.search(r"status: ([A-Z]+)", result["stdout"])
    duration = re.search(r"Query time: (\d+) msec", result["stdout"])
    records = []
    for line in result["stdout"].splitlines():
        match = re.match(r"^(\S+)\s+(\d+)\s+IN\s+(\S+)\s+(.+)$", line)
        if match:
            records.append({"name": match[1], "ttl": int(match[2]),
                            "type": match[3], "value": match[4]})
    flag_lines = re.findall(r"^;; flags: ([^;]*);", result["stdout"], re.MULTILINE)
    truncated = bool(flag_lines and "tc" in flag_lines[-1].split())
    name = domain.rstrip(".").lower()
    visited = set()
    matched = []
    while name not in visited:
        visited.add(name)
        owned = [record for record in records if record["name"].rstrip(".").lower() == name]
        matched = [record for record in owned if record["type"] == "A"]
        if matched:
            break
        alias = next((record for record in owned if record["type"] == "CNAME"), None)
        if not alias:
            break
        name = alias["value"].rstrip(".").lower()
    failed = result["exit"] != 0 or not status or status[1] != "NOERROR" or truncated or not matched
    return {
        "domain": domain, "role": role, "port": port, "tcp": tcp,
        "status": status[1] if status else "NO_DNS_RESPONSE",
        "dns_ms": int(duration[1]) if duration else None,
        "records": records, "exit": result["exit"],
        "probe_failed": failed,
        "truncated": truncated,
        "stderr": result["stderr"], "raw": result["stdout"],
    }

def fingerprints(root):
    values = {}
    for path in ["/tmp/unbound_raw.conf", "/tmp/mosdns.yaml",
                 "/data/dnscrypt-resolvers/dnscrypt.toml"]:
        try:
            with open(root + path, "rb") as f:
                values[path] = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            values[path] = {"error": str(e)}
    return values

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="paopaodns")
    parser.add_argument("--frontend-port", type=int, default=53)
    parser.add_argument("--recursive-port", type=int, default=5301,
                        help="Internal recursive port, or 0 to skip when disabled")
    parser.add_argument("--forward-port", type=int, default=5304,
                        help="Internal encrypted forwarding path port, or 0 to skip")
    parser.add_argument("--control-socket", default="/tmp/uc_raw.sock",
                        help="Absolute Unix control socket path inside PaoPaoDNS")
    parser.add_argument("--domain", action="append",
                        help="Existing public domain to query; repeat for up to 4 domains")
    parser.add_argument("--timeout", type=int, default=6, choices=range(1, 11))
    parser.add_argument("--interval", type=int, default=10, choices=range(0, 61),
                        help="Minimum statistics observation window in seconds")
    parser.add_argument("--output", help="Optional new JSON output file; existing files are not overwritten")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Run this optional collector on the Linux Docker host, not inside the Web UI.")
    if not 1 <= args.frontend_port <= 65535 or not all(0 <= p <= 65535 for p in
                                                       (args.recursive_port, args.forward_port)):
        parser.error("Use ports 1 to 65535 (0 skips an optional internal path).")
    if not args.control_socket.startswith("/") or ".." in args.control_socket.split("/"):
        parser.error("Control socket must be an absolute container path without '..'.")
    domains = args.domain or ["www.qq.com", "www.google.com"]
    label_pattern = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    if len(domains) > 4 or any(len(d.rstrip(".")) > 253 or not all(
            re.fullmatch(label_pattern, label) for label in d.rstrip(".").split(".")) for d in domains):
        parser.error("Use 1 to 4 DNS host names composed of letters, digits, dots and hyphens.")
    if args.output and os.path.exists(args.output):
        parser.error("Output file already exists; choose a new file.")
    info = run(["docker", "inspect", args.container])
    if info["exit"] != 0:
        print(json.dumps({"error": "Cannot inspect container", "detail": info}, indent=2))
        return 1
    try:
        container = json.loads(info["stdout"])[0]
        pid = int(container["State"]["Pid"])
        if pid <= 0 or not container["State"]["Running"]:
            raise ValueError("Container is not running.")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        print(json.dumps({"error": str(e)}, indent=2))
        return 1
    root = "/proc/" + str(pid) + "/root"
    start = time.monotonic()
    before_hashes = fingerprints(root)
    report = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "container": args.container, "container_pid": pid,
        "image": container["Config"]["Image"],
        "mapped_ports": container["NetworkSettings"].get("Ports", {}),
        "method": "Read-only snapshot plus small ordinary DNS probes; no cache clearing. "
                  "Statistics include other LAN traffic. NOERROR alone does not prove "
                  "record freshness or application connectivity.",
        "stats_before": stats(root, args.control_socket), "queries": [],
        "dnscrypt_cache_cleared": False,
    }
    for domain in domains:
        for role, port in [("frontend", args.frontend_port), ("recursive", args.recursive_port),
                           ("encrypted_forward", args.forward_port)]:
            if port:
                report["queries"].append(query(args.container, domain, role, port, args.timeout))
    tcp_port = args.forward_port or args.frontend_port
    tcp_role = "encrypted_forward" if args.forward_port else "frontend"
    report["queries"].append(query(args.container, domains[-1], tcp_role, tcp_port, args.timeout, tcp=True))
    remaining = args.interval - (time.monotonic() - start)
    if remaining > 0:
        time.sleep(remaining)
    report["stats_after"] = stats(root, args.control_socket)
    before, after = report["stats_before"], report["stats_after"]
    report["stats_delta"] = {
        key: after[key] - value
        for key, value in before.items()
        if isinstance(value, (int, float)) and isinstance(after.get(key), (int, float))
        and key in ["total.num.queries", "total.num.cachehits", "total.num.cachemiss",
                    "total.num.prefetch", "total.num.expired", "total.requestlist.exceeded",
                    "time.up"]
    }
    report["observed_counters_reset"] = any(v < 0 for v in report["stats_delta"].values())
    after_hashes = fingerprints(root)
    hashes_read = all(isinstance(x, str)
                      for x in list(before_hashes.values()) + list(after_hashes.values()))
    report["production_configs_unchanged"] = (before_hashes == after_hashes
                                               if hashes_read else None)
    report["config_fingerprints_before"] = before_hashes
    report["config_fingerprints_after"] = after_hashes
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    report["partial"] = (any(q["probe_failed"] for q in report["queries"])
                         or "error" in before or "error" in after or not before or not after
                         or not hashes_read)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        try:
            with open(args.output, "x", encoding="utf-8") as f:
                f.write(result + "\n")
        except OSError as e:
            print("Cannot save output: " + str(e), file=sys.stderr)
            print(result)
            return 2
    print(result)
    return 2 if report["partial"] else 0

if __name__ == "__main__":
    sys.exit(main())
