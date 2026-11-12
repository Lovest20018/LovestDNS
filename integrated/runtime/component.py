"""Execute a fixed component with the environment captured during bootstrap."""
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib

RUN_DIR = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
COMMANDS = {
    "redis": ["/usr/sbin/redis-server", "/tmp/redis.conf", "--daemonize", "no"],
    "unbound-raw": ["/usr/sbin/unbound", "-d", "-p", "-c", "/tmp/unbound_raw.conf"],
    "unbound-forward": ["/usr/sbin/unbound", "-d", "-p", "-c", "/tmp/unbound_forward.conf"],
    "dnscrypt": ["/usr/bin/dnscrypt-proxy", "-config", "/data/dnscrypt-resolvers/dnscrypt.toml"],
    "dnscrypt-socks": ["/usr/bin/dnscrypt-proxy", "-config", "/data/dnscrypt-resolvers/dnscrypt_socks.toml"],
    "mosdns": ["/usr/sbin/mosdns", "start", "-d", "/tmp", "-c", "/tmp/mosdns.yaml"],
    "watcher": ["/usr/sbin/watch_list.sh"],
    "updater": ["/usr/sbin/data_update.sh"],
    "cron": ["/usr/sbin/crond", "-f"],
}


def read_overrides(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r'([_a-zA-Z0-9]+)="([^"\r\n]+)"', line)
        if match:
            values[match[1]] = match[2].replace("'", "")
    return values


def prepare_unbound(role, directory="/tmp"):
    if role not in {"raw", "forward"}:
        raise ValueError("Unknown Unbound role")
    path = Path(directory) / f"unbound_{role}.conf"
    text = path.read_text()
    # Apply to generated runtime files too: an existing /data template predates
    # the patched image template and must still get a separate local endpoint.
    match = re.search(r"(?m)^remote-control:\s*$", text)
    if match:
        following = re.search(r"(?m)^[a-zA-Z][\w-]*:", text[match.end():])
        end = match.end() + following.start() if following else len(text)
        block = text[match.end():end]
        block = re.sub(r"(?m)^\s+(?:control-enable|control-interface|control-use-cert):[^\n]*\n?", "", block)
        text = text[:match.end()] + f'\n    control-enable: yes\n    control-interface: /tmp/uc_{role}.sock\n    control-use-cert: no\n' + block + text[end:]
    else:
        text += f'\nremote-control:\n    control-enable: yes\n    control-interface: /tmp/uc_{role}.sock\n    control-use-cert: no\n'
    path.write_text(text)


def preserve_dnscrypt_defaults(path):
    """Keep the initial independent upgrade on the existing protocol policy."""
    info_path = Path(os.environ.get('SUITE_BUILD_INFO', '/opt/suite/build-info.json'))
    try:
        components = json.loads(info_path.read_text()).get('components') or {}
    except FileNotFoundError:
        return
    if 'dnscrypt' not in components.get('selected', []):
        return
    path = Path(path)
    value = path.read_text()
    settings = tomllib.loads(value)
    # Insert defaults at TOML's top level, preserving explicit user choices.
    defaults = {'pqdnscrypt': 'false', 'lb_strategy': "'p2'"}
    missing = [f'{name} = {setting}' for name, setting in defaults.items() if name not in settings]
    if missing:
        path.write_text('# LovestDNS: preserve pre-upgrade protocol and resolver selection.\n' + '\n'.join(missing) + '\n' + value)


def main():
    action = sys.argv[1]
    if action == "capture-env":
        path = RUN_DIR / "core-env.json"
        path.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
        path.chmod(0o600)
        return
    if action == "wait-redis":
        from runtime.observer import redis_stats
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if redis_stats()["loading"] == 0:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise SystemExit("Redis did not become ready within 120 seconds")
    if action == "prepare-unbound":
        prepare_unbound(sys.argv[2])
        return
    command = COMMANDS.get(action)
    if command is None:
        raise SystemExit("Unknown component")
    if action in {'dnscrypt', 'dnscrypt-socks'}:
        name = 'dnscrypt_socks.toml' if action == 'dnscrypt-socks' else 'dnscrypt.toml'
        preserve_dnscrypt_defaults(Path('/data/dnscrypt-resolvers') / name)
    env = json.loads((RUN_DIR / "core-env.json").read_text())
    if action == "mosdns":
        active = RUN_DIR / "mosdns-env.json"
        if active.exists():
            env = json.loads(active.read_text())
        else:
            env.update(read_overrides("/data/custom_env.ini"))
            active.write_text(json.dumps(env))
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
