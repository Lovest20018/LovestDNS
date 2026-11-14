"""Bounded native rule updates; executable releases remain immutable."""
import hashlib
import http.client
import json
import lzma
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request

from runtime.locking import maintenance_lock
from runtime.maintenance import CoreActions, atomic_file
from native.backup import create as runtime_backup

ASSET_LIMIT = 16 * 1024 * 1024
EXPANDED_LIMIT = 32 * 1024 * 1024
CYCLE_SECONDS = 240
REQUEST_SECONDS = 15
PERIODS = {"daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}
STATUS_NAME = "rule-update.json"


class RuleUpdateFailed(RuntimeError):
    def __init__(self, message, recovery=None):
        super().__init__(message)
        self.recovery = recovery


def github_sources(repository, filename):
    return [
        (f"https://raw.githubusercontent.com/{repository}/main/{filename}.sha256sum",
         f"https://raw.githubusercontent.com/{repository}/main/{filename}"),
        (f"https://cdn.jsdelivr.net/gh/{repository}@main/{filename}.sha256sum",
         f"https://cdn.jsdelivr.net/gh/{repository}@main/{filename}"),
        (f"https://api.github.com/repos/{repository}/contents/{filename}.sha256sum?ref=main",
         f"https://api.github.com/repos/{repository}/contents/{filename}?ref=main"),
    ]


RULES = {
    "root_hints": ("named.cache", "md5", [
        ("https://www.internic.net/domain/named.cache.md5",
         "https://www.internic.net/domain/named.cache")]),
    "country": ("Country-only-cn-private.mmdb.xz", "sha256",
                github_sources("kkkgo/Country-only-cn-private.mmdb", "Country-only-cn-private.mmdb.xz")),
    "trackers": ("trackerslist.txt.xz", "sha256",
                 github_sources("kkkgo/all-tracker-list", "trackerslist.txt.xz")),
    "global_mark": ("global_mark.dat", "sha256",
                    github_sources("kkkgo/PaoPao-Pref", "global_mark.dat")),
}


def read(path, limit=ASSET_LIMIT):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Rule input must be a regular file")
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("Rule input exceeds size limit")
    return value


def write(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Rule destination must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_file(path, value)


def load_status(state):
    try:
        value = json.loads(read(Path(state) / STATUS_NAME, 64 * 1024))
        if value.get("schema_version") == 1:
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def fetch(url, limit, deadline, core, env):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Rule update deadline reached")
    timeout = min(REQUEST_SECONDS, remaining)
    if env.get("SOCKS5"):
        # Retain the upstream SOCKS transport without adding Python packages.
        # This command has fixed URLs, a bounded output file and a deadline.
        import resource
        def bounded_file():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
        with tempfile.TemporaryDirectory(prefix="rule-fetch-") as directory:
            path = Path(directory) / "download"
            subprocess.run([str(core / "mosdns"), "curl", url, env["SOCKS5"].strip('"'), str(path)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=timeout, preexec_fn=bounded_file)
            return read(path, limit)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={
        "User-Agent": "LovestDNS-native-rule-updater/1",
        "Accept": "application/vnd.github.raw+json",
    })
    chunks, length = [], 0
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200 or not response.geturl().startswith("https://"):
            raise ValueError("Unexpected rule download response")
        while block := response.read(min(64 * 1024, limit + 1 - length)):
            length += len(block)
            if length > limit:
                raise ValueError("Rule download exceeds size limit")
            if time.monotonic() > deadline:
                raise TimeoutError("Rule update deadline reached")
            chunks.append(block)
    return b"".join(chunks)


def checksum(value, algorithm):
    length = hashlib.new(algorithm).digest_size * 2
    matches = re.findall(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{" + str(length).encode() + rb"}(?![0-9a-fA-F])", value)
    if not matches:
        raise ValueError("Rule checksum response is empty or invalid")
    return matches[0].decode("ascii").lower()


def verified_asset(name, local, core, env, deadline, downloader=fetch):
    _, algorithm, sources = RULES[name]
    old = read(local) if local.is_file() else None
    for digest_url, url in sources:
        # A broken SOCKS endpoint must not disable the direct fallback.
        transports = [env, {**env, "SOCKS5": ""}] if env.get("SOCKS5") else [env]
        for transport in transports:
            try:
                expected = checksum(downloader(digest_url, 4096, deadline, core, transport), algorithm)
                if old is not None and hashlib.new(algorithm, old).hexdigest() == expected:
                    return old, {"asset": name, "sha256": hashlib.sha256(old).hexdigest(),
                                 "source": url, "downloaded": False}
                value = downloader(url, ASSET_LIMIT, deadline, core, transport)
                if not value or hashlib.new(algorithm, value).hexdigest() != expected:
                    raise ValueError("Rule download checksum mismatch")
                return value, {"asset": name, "sha256": hashlib.sha256(value).hexdigest(),
                               "source": url, "downloaded": True}
            except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
                # No URL supplied by a client and no proxy credentials enter logs.
                print(f"Rule update {name}: {type(error).__name__}; trying next source", flush=True)
    raise RuleUpdateFailed(f"No verified source available for {name}")


def root_hints(value):
    if (len(value) > 256 * 1024 or b"ROOT-SERVERS.NET" not in value.upper()
            or not re.search(rb"(?im)^\s*\.\s+[0-9]+\s+(?:IN\s+)?NS\s+", value)):
        raise ValueError("Root hints content is invalid")
    return value


def expand(value):
    # The locked upstream GeoIP stream declares a 64 MiB dictionary; decoder
    # bookkeeping also counts towards memlimit. Keep a bounded allowance for it.
    decoder = lzma.LZMADecompressor(memlimit=128 * 1024 * 1024)
    result = decoder.decompress(value, max_length=EXPANDED_LIMIT + 1)
    if len(result) > EXPANDED_LIMIT or not decoder.eof or decoder.unused_data:
        raise ValueError("Compressed rule is invalid or exceeds expansion limit")
    return result


def desired_files(values, core):
    """Validate all fetched assets before changing the first active file."""
    result = {}
    assets = core.data / "rule-assets"
    for name, value in values.items():
        filename = RULES[name][0]
        if name == "root_hints":
            root_hints(value)
            result[core.temporary / "named.cache"] = value
            result[assets / filename] = value
        elif name == "country":
            decoded = expand(value)
            if b"\xab\xcd\xefMaxMind.com" not in decoded[-128 * 1024:]:
                raise ValueError("GeoIP content is invalid")
            result[assets / filename] = value
            result[core.data / "Country-only-cn-private.mmdb"] = decoded
        elif name == "trackers":
            decoded = expand(value).decode("utf-8")
            target = core.data / "trackerslist.txt"
            current = read(target, EXPANDED_LIMIT).decode("utf-8") if target.is_file() else ""
            # Preserve user additions, matching the existing upstream union.
            lines = sorted({line for line in (decoded + "\n" + current).splitlines()
                            if re.fullmatch(r"[a-z]+://.+", line)})
            if not lines:
                raise ValueError("Tracker content is empty or invalid")
            merged = ("\n".join(lines) + "\n").encode()
            if len(merged) > EXPANDED_LIMIT:
                raise ValueError("Merged tracker content exceeds size limit")
            result[assets / filename] = value
            result[target] = merged
        elif name == "global_mark":
            if len(value) <= 10000:
                raise ValueError("Global marker content is invalid")
            # MosDNS's format appends a 1024-byte NUL-padded SHA-512 trailer
            # to the XZ stream. Validate it before the legacy loader sees it.
            embedded = checksum(value[-1024:], "sha512")
            if hashlib.sha512(value[:-1024]).hexdigest() != embedded:
                raise ValueError("Global marker embedded checksum mismatch")
            expand(value[:-1024])
            result[core.data / filename] = value
    return result


HASH_INPUTS = {
    "named": ("temporary", "named.cache"),
    "Country": ("data", "Country-only-cn-private.mmdb"),
    **{name: ("data", name + ".txt") for name in (
        "force_recurse_list", "force_cn_list", "force_dnscrypt_list", "force_nocn_list",
        "force_forward_list", "force_ttl_rules", "custom_cn_mark", "trackerslist")},
    "custom_env": ("data", "custom_env.ini"),
}


def reload_environment(core, env):
    result = {**env, "SUITE_RELOAD_LOCKED": "1"}
    for key, (group, name) in HASH_INPUTS.items():
        path = getattr(core, group) / name
        result[key] = hashlib.md5(read(path, EXPANDED_LIMIT)).hexdigest() if path.is_file() else "empty_file"
    return result


def apply(core, home, env, values):
    """Publish under the normal maintenance guard, then rearm inode watches."""
    with maintenance_lock(core.state, timeout=90):
        core.environment()
        before = core.states()
        if before.get("watcher", {}).get("statename") != "RUNNING":
            raise RuleUpdateFailed("Rule watcher is not running")
        files = desired_files(values, core)
        previous = {path: read(path, EXPANDED_LIMIT) if path.is_file() else None for path in files}
        changed = {path: value for path, value in files.items() if previous[path] != value}
        if not changed:
            return []
        active = [path for path in changed if path.parent != core.data / "rule-assets"]
        if not active:
            for path, value in changed.items():
                write(path, value)
            return [str(p) for p in changed]
        old_env = reload_environment(core, env)
        flag = core.temporary / "global_mark.flag"
        previous_flag = read(flag, 64) if flag.is_file() else None
        backup = runtime_backup(core)
        watcher_stopped = False
        published = False
        try:
            core.control("stop", "watcher")
            watcher_stopped = True
            for path, value in changed.items():
                write(path, value)
                published = True
            if core.data / "global_mark.dat" in active:
                write(flag, b"ok\n")
            # The upstream raw reload ends with a BusyBox-style ps probe
            # which returns false on Debian despite a successful restart.
            # Reuse its rule generation only for MosDNS and control Unbound
            # through Supervisor with the same bounded readiness checks.
            if env.get("CNAUTO") != "no" and any(path.parent == core.data for path in active):
                subprocess.run(
                    ["/bin/bash", str(home / "watch_list.sh"), "suite-reload-force"],
                    env=old_env, timeout=60, check=True,
                )
            if core.temporary / "named.cache" in active:
                from native.restart import control
                control("stop", "unbound-raw")
                control("start", "unbound-raw")
            required = ["unbound-raw"] + (["mosdns"] if env.get("CNAUTO") != "no" else [])
            for name in required:
                core.wait_running(name)
            core.probe()
            core.control("start", "watcher")
            watcher_stopped = False
            core.wait_running("watcher")
            return [str(p) for p in changed]
        except Exception as error:
            recovery = {"attempted": published, "files_restored": False,
                        "runtime_restored": False, "service_responded": False}
            try:
                if published:
                    for path, value in previous.items():
                        if value is None:
                            path.unlink(missing_ok=True)
                        else:
                            write(path, value)
                    recovery["files_restored"] = True
                    backup.restore()
                    recovery["runtime_restored"] = True
                    if previous_flag is None:
                        flag.unlink(missing_ok=True)
                    else:
                        write(flag, previous_flag)
                    from native.restart import control
                    for name in ("mosdns", "unbound-raw"):
                        if name == "mosdns" and env.get("CNAUTO") == "no":
                            continue
                        if core.states().get(name, {}).get("statename") in {"RUNNING", "STARTING", "BACKOFF"}:
                            control("stop", name)
                        control("start", name)
                        core.wait_running(name)
                    core.probe()
                    recovery["service_responded"] = True
            except Exception:
                recovery["error"] = "Recovery incomplete; inspect component logs on the host"
            raise RuleUpdateFailed("Rule apply failed; inspect recovery result", recovery) from error
        finally:
            if watcher_stopped:
                core.control("start", "watcher")


def update(home, env, actions=None, downloader=fetch):
    core = actions or CoreActions(Path(os.environ["SUITE_STATE_DIR"]),
                                  Path(os.environ["SUITE_RUN_DIR"]))
    status_path = core.state / STATUS_NAME
    previous = load_status(core.state)
    now = time.time()
    report = {"schema_version": 1, "phase": "running", "started_at": now,
              "last_success_at": previous.get("last_success_at"),
              "consecutive_failures": int(previous.get("consecutive_failures", 0)),
              "assets": []}
    write(status_path, json.dumps(report).encode())
    try:
        deadline = time.monotonic() + CYCLE_SECONDS
        names = ["root_hints"]
        if env.get("CNAUTO") != "no":
            names += ["country"]
            if env.get("CN_TRACKER") == "yes":
                names += ["trackers"]
            if env.get("USE_MARK_DATA") == "yes":
                names += ["global_mark"]
        values = {}
        for name in names:
            local = (core.data / RULES[name][0] if name == "global_mark"
                     else core.data / "rule-assets" / RULES[name][0])
            if not local.exists():
                local = home / RULES[name][0]
            value, receipt = verified_asset(name, local, home, env, deadline, downloader)
            values[name] = value
            report["assets"].append(receipt)
        # Bootstrap starts the updater just before setting ready.
        ready_deadline = time.monotonic() + 45
        while not (core.run / "ready").exists():
            if time.monotonic() >= ready_deadline:
                raise RuleUpdateFailed("Core initialization did not finish")
            time.sleep(.5)
        report["changed_files"] = apply(core, home, env, values)
        completed = time.time()
        report.update(phase="succeeded", completed_at=completed, last_success_at=completed,
                      consecutive_failures=0, next_attempt_at=completed + PERIODS.get(env.get("UPDATE"), 86400))
        write(core.state / "last-periodic-update", str(completed).encode())
        print("Rule update succeeded; all requested assets verified and applied", flush=True)
        return report
    except Exception as error:
        failures = report["consecutive_failures"] + 1
        retry = min(3600, 60 * 2 ** min(failures - 1, 6))
        report.update(phase="failed", completed_at=time.time(), consecutive_failures=failures,
                      next_attempt_at=time.time() + retry,
                      error=type(error).__name__ + ": rule update failed; inspect updater log")
        if isinstance(error, RuleUpdateFailed) and error.recovery is not None:
            report["recovery"] = error.recovery
        print(f"Rule update failed ({type(error).__name__}); retry in {retry}s; "
              f"recovery={report.get('recovery', 'no active files changed')}", flush=True)
        raise RuleUpdateFailed(report["error"]) from error
    finally:
        write(status_path, json.dumps(report, ensure_ascii=False).encode())


def next_attempt(state, period, now):
    status = load_status(state)
    if status.get("phase") == "failed":
        value = status.get("next_attempt_at")
        if isinstance(value, (int, float)) and math.isfinite(value):
            return min(value, now + 3600)
    if status.get("phase") == "running":
        value = status.get("started_at")
        if isinstance(value, (int, float)) and math.isfinite(value):
            return min(value + CYCLE_SECONDS + 180, now + 3600)
    try:
        last = float(read(Path(state) / "last-periodic-update", 64))
        if not math.isfinite(last):
            raise ValueError("Invalid update timestamp")
    except (OSError, ValueError):
        last = now
    return last + period
