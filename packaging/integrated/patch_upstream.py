"""Version-specific integration patch. Fail closed when upstream bytes change."""
import argparse
import hashlib
import json
from pathlib import Path

CTL = "/opt/venv/bin/supervisorctl -c /opt/suite/supervisord.conf"
PY = "/opt/venv/bin/python -m runtime.component"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Expected one upstream anchor: {old[:90]!r}")
    return text.replace(old, new, 1)


def patch_init(text):
    text = replace_once(text, "chmod -R 777 /data", "# Directory permissions are managed by the integrated entrypoint.")
    a = text.index('if [ ! -f /new.lock ]; then')
    b = text.index('\nfi', a) + 3
    text = text[:a] + '# Preserve existing data and user templates across container recreation.' + text[b:]
    text = replace_once(text, '    crond\n', f'    {CTL} start cron || exit 1\n')
    text = replace_once(text, 'cat /tmp/env.conf', '# Environment diagnostics remain private; do not copy credentials into logs.')
    # Honour cgroup memory limits as well as host availability. Keep legacy tuning
    # below intact while reserving memory for the web and collector processes.
    a = text.index("if grep -q 'MemAvailable' /proc/meminfo; then")
    b = text.index('\nprefPC=1', a)
    text = text[:a] + 'MEMSIZE=$(/opt/venv/bin/python -m runtime.resources)\n' + text[b:]
    a = text.index('redis-server /tmp/redis.conf\n')
    b = text.index('\nsed "s/{CORES}', a)
    text = text[:a] + f'{PY} capture-env\n{CTL} start redis || exit 1\n{PY} wait-redis || exit 1\n' + text[b:]
    # cron also needs the captured environment, so defer it until capture-env.
    text = replace_once(text, f'    {CTL} start cron || exit 1\n', '    : # cron starts after environment capture\n')
    text = replace_once(text, f'{PY} capture-env\n', f'{PY} capture-env\nif [ "$UPDATE" != "no" ]; then {CTL} start cron || exit 1; fi\n')
    text = replace_once(text, '    dnscrypt-proxy -config /data/dnscrypt-resolvers/dnscrypt.toml >/dev/null 2>&1 &',
                        f'    {CTL} start dnscrypt || exit 1')
    text = replace_once(text, '    dnscrypt-proxy -config /data/dnscrypt-resolvers/dnscrypt_socks.toml >/dev/null 2>&1 &',
                        f'    if echo "$SOCKS5" | grep -Eq ":[0-9]+$"; then {CTL} start dnscrypt-socks || exit 1; fi')
    text = replace_once(text, '    unbound -c /tmp/unbound_forward.conf -p',
                        f'    {PY} prepare-unbound forward || exit 1\n    unbound-checkconf /tmp/unbound_forward.conf || exit 1\n    {CTL} start unbound-forward || exit 1')
    text = replace_once(text, '    mosdns AddMod', '    rm -f /tmp/mosdns_mod.yaml\n    mosdns AddMod\n    test -s /tmp/mosdns_mod.yaml || exit 1')
    text = replace_once(text, '    mosdns start -d /tmp -c /tmp/mosdns.yaml &', f'    {CTL} start mosdns || exit 1')
    text = replace_once(text, 'unbound -c /tmp/unbound_raw.conf -p',
                        f'{PY} prepare-unbound raw || exit 1\nunbound-checkconf /tmp/unbound_raw.conf || exit 1\n{CTL} start unbound-raw || exit 1')
    text = replace_once(text, '/usr/sbin/watch_list.sh &', f'{CTL} start watcher || exit 1')
    text = replace_once(text, '    /usr/sbin/data_update.sh &', f'    {CTL} start updater || exit 1')
    text = replace_once(text, 'tail -f /dev/null', 'touch /run/paopaodns-suite/ready')
    return text


def patch_watcher(text):
    text = replace_once(text, 'reload_dns() {\n', '''reload_dns() {
    if [ "$SUITE_RELOAD_LOCKED" != "1" ]; then
        /opt/venv/bin/python -m runtime.legacy_reload "$1"
        return $?
    fi
''')
    text = replace_once(text, 'if [ "$1" = "reload_dns" ]; then\n    reload_dns force',
                        'if [ "$1" = "reload_dns" ] || [ "$1" = "suite-reload-force" ]; then\n    reload_dns force')
    text = replace_once(text, 'while true; do\n    file_list=', '''if [ "$1" = "suite-reload-check" ]; then
    reload_dns check
    exit $?
fi
while true; do
    file_list=''')
    # Preserve the requested force flag; upstream overwrites it inside CNAUTO.
    text = replace_once(text, '    if [ "$CNAUTO" != "no" ]; then\n        export reload_mosdns=0',
                        '    if [ "$CNAUTO" != "no" ]; then\n        : # Keep force reload flag')
    a = text.index('                    if ps | grep dnscrypt-proxy')
    b = text.index('\n                fi', a)
    text = text[:a] + f'                    {CTL} restart dnscrypt || return 1' + text[b:]
    a = text.index('            while ps | grep -v grep | grep -q "mosdns.yaml"; do')
    b = text.index('\n            echo "mosdns reload..."', a)
    text = text[:a] + text[b:]
    text = replace_once(text, '            /usr/sbin/mosdns start -d /data -c /tmp/mosdns.yaml &',
                        f'            rm -f /run/paopaodns-suite/mosdns-env.json\n            {CTL} restart mosdns || return 1')
    a = text.index('        while ps | grep -v grep | grep -q unbound_raw; do')
    b = text.index('\n        echo "unbound reload..."', a)
    text = text[:a] + text[b:]
    text = replace_once(text, '        /usr/sbin/unbound -c /tmp/unbound_raw.conf >/dev/null 2>&1 &',
                        f'        {CTL} restart unbound-raw || return 1')
    return text


def patch(root, lock):
    root = Path(root)
    for filename, checksum in lock["verified_source_files"].items():
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Upstream image/source mismatch: {filename}")
    transforms = {"init.sh": patch_init, "watch_list.sh": patch_watcher,
        "reload.sh": lambda text: replace_once(text, '/usr/sbin/mosdns AddMod',
            'rm -f /tmp/mosdns_mod.yaml\n/usr/sbin/mosdns AddMod\ntest -s /tmp/mosdns_mod.yaml || exit 1'),
        "unbound.conf": lambda text: replace_once(replace_once(text,
            '#CNAUTO\tcontrol-enable: no', '#CNAUTO\tcontrol-enable: yes'),
            '#RAWDNS\tcontrol-use-cert: "no"',
            '#RAWDNS\tcontrol-use-cert: "no"\n#CNAUTO\tcontrol-interface: /tmp/uc_forward.sock\n#CNAUTO\tcontrol-use-cert: "no"')}
    for name, transform in transforms.items():
        (root / name).write_text(transform((root / name).read_text()), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/usr/sbin")
    parser.add_argument("--lock", default="/opt/suite/upstream.lock.json")
    args = parser.parse_args()
    patch(args.root, json.loads(Path(args.lock).read_text()))
