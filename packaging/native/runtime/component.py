"""Execute exact upstream binaries through their bundled musl loader."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from runtime import component as shared
from native.environment import validate
from runtime.configuration import read_bounded
from runtime.maintenance import atomic_file


def endpoint(address, port):
    ipaddress.ip_address(address)
    if not 1 <= int(port) <= 65535:
        raise ValueError("Invalid DNS port")
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def render_mosdns():
    path = Path("/tmp/mosdns.yaml")
    value = path.read_text()
    address = endpoint(os.environ["DNS_BIND_ADDRESS"], os.environ["SUITE_DNS_PORT"])
    value, count = re.subn(r"(?m)^(\s+listen:)\s+:53\s*$", lambda m: f"{m[1]} {json.dumps(address)}", value)
    if count != 2:
        raise ValueError("MosDNS listen template differs from the locked baseline")
    path.write_text(value)


def prepare_unbound(role):
    shared.prepare_unbound(role)
    path = Path(f"/tmp/unbound_{role}.conf")
    value = path.read_text().replace('/etc/unbound/named.cache', '/tmp/named.cache')
    value = freeze_unbound_custom(value)
    # The pinned binary's compiled working directory belongs to Alpine. Apply
    # this to generated files too so imported upstream templates work unchanged.
    value, directories = re.subn(r'(?m)^\s*directory:[^\n]*$', '    directory: "/tmp"', value)
    if not directories:
        value = re.sub(r'(?m)^server:\s*$', 'server:\n    directory: "/tmp"', value, count=1)
    if role == "raw" and os.environ.get("CNAUTO") == "no":
        address = os.environ["DNS_BIND_ADDRESS"]
    elif role == "forward":
        address = os.environ.get("FORWARD_BIND_ADDRESS", "127.0.0.1")
    else:
        address = "127.0.0.1"
    ipaddress.ip_address(address)
    value, count = re.subn(r"(?m)^\s*interface:\s+0\.0\.0\.0\s*$", lambda _: f"    interface: {address}", value)
    if count != 1:
        raise ValueError("Unbound interface template differs from the locked baseline")
    path.write_text(value)


def freeze_unbound_custom(value, source='/data/unbound_custom.conf', target='/tmp/unbound_custom.conf'):
    if '/data/unbound_custom.conf' not in value:
        return value
    # Unbound reads includes each time it starts. Point running configs at the
    # generated copy so a saved invalid file cannot break rollback of the old
    # runtime. The restart worker snapshots this copy with the other /tmp files.
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Unbound custom configuration must be a regular file')
    atomic_file(target, read_bounded(source))
    return value.replace('/data/unbound_custom.conf', str(target))


def prepare_dnscrypt(socks=False):
    path = Path('/data/dnscrypt-resolvers') / ('dnscrypt_socks.toml' if socks else 'dnscrypt.toml')
    value, count = re.subn(r'(?m)^listen_addresses\s*=.*$',
                          f"listen_addresses = ['127.0.0.1:{5303 if socks else 5302}']", path.read_text())
    if count != 1:
        raise ValueError('DNSCrypt listen template differs from the locked baseline')
    path.write_text(value)
    shared.preserve_dnscrypt_defaults(path)


def update(core, env):
    import fcntl
    from native.rule_updates import update as update_rules
    # The boot update and periodic scheduler cannot modify rule data together.
    path = Path(os.environ["SUITE_RUN_DIR"]) / "updater.lock"
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        update_rules(core, env)
        return True


def scheduler(core, env):
    from native.rule_updates import load_status, next_attempt
    period = {"daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}.get(env.get("UPDATE"))
    if period is None:
        raise ValueError("UPDATE must be no, daily, weekly or monthly")
    state = Path(os.environ["SUITE_STATE_DIR"])
    due = next_attempt(state, period, time.time())
    while True:
        # Re-read the boot updater's result. A failed initial fetch must retry
        # soon even if a previous weekly update had recently succeeded.
        if load_status(state):
            due = next_attempt(state, period, time.time())
        if time.time() >= due:
            try:
                # Use the named Supervisor process so retries and its eventual
                # exit status remain visible in the existing updater log/UI.
                import xmlrpc.client
                from runtime.observer import UnixTransport
                with xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=UnixTransport()) as rpc:
                    rpc.supervisor.startProcess("updater", False)
            except (OSError, xmlrpc.client.Error):
                print("Rule updater is busy or unavailable; retrying scheduler in 30s", flush=True)
        time.sleep(30)


def main():
    action = sys.argv[1]
    if action == "prepare-unbound":
        prepare_unbound(sys.argv[2])
        return
    if action == "render-mosdns":
        render_mosdns()
        return
    if action in {"capture-env", "wait-redis"}:
        shared.main()
        return
    home = Path(os.environ["LOVESTDNS_HOME"]).resolve()
    core = home / "core"
    run = Path(os.environ["SUITE_RUN_DIR"])
    env = json.loads((run / "core-env.json").read_text())
    if action == "cron":
        scheduler(core, env)
        return
    if action == "updater":
        update(core, env)
        return
    commands = {"redis": [str(core / "redis-server"), "/tmp/redis.conf", "--daemonize", "no"],
                "unbound-raw": [str(core / "unbound"), "-d", "-p", "-c", "/tmp/unbound_raw.conf"],
                "unbound-forward": [str(core / "unbound"), "-d", "-p", "-c", "/tmp/unbound_forward.conf"],
                "dnscrypt": [str(core / "dnscrypt-proxy"), "-config", "/data/dnscrypt-resolvers/dnscrypt.toml"],
                "dnscrypt-socks": [str(core / "dnscrypt-proxy"), "-config", "/data/dnscrypt-resolvers/dnscrypt_socks.toml"],
                "mosdns": [str(core / "mosdns"), "start", "-d", "/tmp", "-c", "/tmp/mosdns.yaml"],
                "watcher": ["/bin/bash", str(core / "watch_list.sh")]}
    if action not in commands:
        raise ValueError("Unsupported native component")
    if action in {'dnscrypt', 'dnscrypt-socks'}:
        prepare_dnscrypt(action == 'dnscrypt-socks')
    if action == "mosdns":
        active = run / "mosdns-env.json"
        if active.exists():
            env = json.loads(active.read_text())
        else:
            overrides = shared.read_overrides("/data/custom_env.ini")
            validate(overrides)
            env.update(overrides)
            active.write_text(json.dumps(env))
    command = commands[action]
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
