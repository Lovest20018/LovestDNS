"""Container preparation; preserve user files and start the process supervisor."""
import hashlib
import json
import os
from pathlib import Path
import time

CONFIG_FILES = ("custom_env.ini", "custom_mod.yaml", "unbound_custom.conf", "redis.conf",
                "unbound.conf", "mosdns.yaml", "dnscrypt.toml", "force_dnscrypt_list.txt",
                "force_recurse_list.txt", "force_forward_list.txt", "force_ttl_rules.txt",
                "custom_cn_mark.txt", "trackerslist.txt")


def preserve_inputs(data, state):
    marker = state / "initial-inputs.json"
    if marker.exists():
        return
    destination = state / "initial-inputs"
    destination.mkdir(exist_ok=True, mode=0o700)
    inventory = {}
    for name in CONFIG_FILES:
        path = data / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError(f"Cannot safely preserve configuration input: {name}")
        content = path.read_bytes()
        target = destination / name
        # A failed first boot can be retried without overwriting the first copy.
        if not target.exists():
            with target.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        inventory[name] = {"sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                           "bytes": target.stat().st_size}
    inventory["redis_dns_v2.rdb"] = {"present": (data / "redis_dns_v2.rdb").exists(),
                                      "copied": False}
    marker.write_text(json.dumps({"created_at": time.time(), "files": inventory,
        "scope": "Known configuration inputs only; not a full data/cache backup"}, indent=2) + "\n")


def main():
    os.umask(0o077)
    data = Path("/data")
    state = Path(os.environ.get("SUITE_STATE_DIR", "/var/lib/paopaodns-suite"))
    run = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
    # Process and log paths are fixed by supervisor and the patched core.
    if str(state) != "/var/lib/paopaodns-suite" or str(run) != "/run/paopaodns-suite":
        raise SystemExit("Suite runtime paths are fixed; mount volumes at the documented paths")
    if not os.environ.get("WEB_UI_TOKEN") and os.environ.get("WEB_UI_ALLOW_NO_AUTH") != "true":
        raise SystemExit("Set WEB_UI_TOKEN before starting the integrated edition")
    if data.resolve() == state.resolve() or data.resolve() in state.resolve().parents:
        raise SystemExit("Suite state must be outside the upstream /data directory")
    for path in (data, state, state / "logs", run):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    preserve_inputs(data, state)
    for name in ("ready", "core-env.json", "mosdns-env.json", "supervisor.sock", "observer.sock", "manager.sock", "query-recorder.sock"):
        (run / name).unlink(missing_ok=True)
    os.execv("/opt/venv/bin/supervisord", ["supervisord", "-c", "/opt/suite/supervisord.conf"])


if __name__ == "__main__":
    main()
