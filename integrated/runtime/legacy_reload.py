"""Serialize upstream watcher reloads with API saves and maintenance actions."""
import os
from pathlib import Path
import subprocess
import sys

from runtime.locking import maintenance_lock


def main():
    action = sys.argv[1]
    if action not in {"force", "check"}:
        raise SystemExit("Unsupported reload action")
    state = Path(os.environ.get("SUITE_STATE_DIR", "/var/lib/paopaodns-suite"))
    # File watching is already coalesced by the upstream loop. A bounded wait
    # prevents a stopped worker or slow maintenance job from hanging it forever.
    with maintenance_lock(state, timeout=90):
        env = {**os.environ, "SUITE_RELOAD_LOCKED": "1"}
        try:
            result = subprocess.run(["/bin/sh", "/usr/sbin/watch_list.sh", "suite-reload-" + action],
                                    env=env, timeout=45, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SystemExit("Watcher reload timed out") from exc
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
