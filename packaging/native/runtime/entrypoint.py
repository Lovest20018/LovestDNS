"""Prepare the systemd service's private mounts and start the process supervisor."""
import json
import os
from pathlib import Path
import shutil
import sys

from runtime.entrypoint import preserve_inputs
from native.supervisor import configuration


def main():
    os.umask(0o077)
    home = Path(os.environ["LOVESTDNS_HOME"]).resolve()
    state = Path(os.environ["SUITE_STATE_DIR"])
    run = Path(os.environ["SUITE_RUN_DIR"])
    if not os.environ.get("WEB_UI_TOKEN"):
        raise SystemExit("WEB_UI_TOKEN is required")
    for path in (state, state / "logs", run):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # /data is a systemd BindPaths alias. /tmp belongs to PrivateTmp, never to
    # the host or to another DNS instance. No chroot or container is involved.
    data = Path("/data")
    if not data.is_dir() or os.environ.get("LOVESTDNS_SYSTEMD") != "1":
        raise SystemExit("Start this runtime through the installed systemd unit")
    preserve_inputs(data, state)
    for name in ("ready", "core-env.json", "mosdns-env.json", "supervisor.sock", "observer.sock", "manager.sock"):
        (run / name).unlink(missing_ok=True)
    shutil.copyfile(home / "core/named.cache", "/tmp/named.cache")
    os.environ["DNSPORT"] = os.environ["SUITE_DNS_PORT"]
    config = run / "supervisord.conf"
    config.write_text(configuration(home, state, run, os.environ["WEB_BIND_ADDRESS"], int(os.environ["WEB_HOST_PORT"])))
    os.execv(sys.executable, [sys.executable, "-m", "supervisor.supervisord", "-c", str(config)])


if __name__ == "__main__":
    main()
