"""Adapt the verified integration patch to Debian without touching host files."""
from pathlib import Path

from patch_upstream import replace_once


def adapt_mark_checksum(text):
    # The upstream checksum header is padded with NUL bytes. GNU grep
    # suppresses matching text in binary files unless -a is explicit.
    return replace_once(text, 'sp_sha_hash=$(grep -Eo', 'sp_sha_hash=$(grep -aEo')


def adapt_root_hints_update(text):
    # The native root-hints file also lives in /tmp. Keep the download separate:
    # the upstream updater truncates/removes update_file_down after checking it.
    return replace_once(text, 'update_file_down="/tmp/named.cache"',
                        'update_file_down="/tmp/named.cache.download"')


def adapt(directory):
    directory = Path(directory)
    init = (directory / "init.sh").read_text()
    start = init.index('if [ "$UPDATE" != "no" ]; then\n')
    end = init.index('\nfree -m', start)
    init = init[:start] + '# Periodic updates are managed by native.component, not host cron.\n' + init[end:]
    init = replace_once(init, 'echo "nameserver 127.0.0.1" >/etc/resolv.conf\necho "nameserver 223.5.5.5" >>/etc/resolv.conf\necho "nameserver 1.0.0.1" >>/etc/resolv.conf',
                        '# Keep the Debian host resolver unchanged.')
    init = replace_once(init, '    # Add Mods\n',
                        '    "${LOVESTDNS_HOME}/venv/bin/python" -m native.component render-mosdns || exit 1\n    # Add Mods\n')
    init = init.replace('touch /run/paopaodns-suite/ready', 'touch "$SUITE_RUN_DIR/ready"')
    init = init.replace('=====PaoPaoDNS docker start=====', '=====LovestDNS native start=====')
    init = init.replace('\nps\n', '\n# Components are listed by the runtime observer.\n')
    anchor = 'grep -Eo "^[_a-zA-Z0-9]+='
    init = replace_once(init, anchor, '"${LOVESTDNS_HOME}/venv/bin/python" -m native.environment /data/custom_env.ini || exit 1\n' + anchor)
    (directory / "init.sh").write_text(init)
    update = (directory / "data_update.sh").read_text()
    start = update.index('update-ca-certificates >/dev/null 2>&1')
    end = update.index('\nupdate_file=', start)
    update = update[:start] + '# Debian CA updates are owned by apt, not apk.\n' + update[end:]
    (directory / "data_update.sh").write_text(update)
    for name in ("init.sh", "watch_list.sh", "reload.sh", "data_update.sh"):
        path = directory / name
        text = path.read_text()
        text = text.replace('#!/bin/sh', '#!/bin/bash', 1)
        text = text.replace('\n. /etc/profile\n', '\n# Use the isolated service environment.\n')
        text = text.replace('/usr/sbin/', '${LOVESTDNS_HOME}/core/')
        text = text.replace('/opt/venv/bin/', '${LOVESTDNS_HOME}/venv/bin/')
        text = text.replace('/opt/suite/supervisord.conf', '${SUITE_RUN_DIR}/supervisord.conf')
        text = text.replace('/run/paopaodns-suite/', '${SUITE_RUN_DIR}/')
        text = text.replace('/etc/unbound/named.cache', '/tmp/named.cache')
        text = text.replace('0.0.0.0:5303', '127.0.0.1:5303')
        text = text.replace('-m runtime.component', '-m native.component')
        text = text.replace('-m runtime.legacy_reload', '-m native.legacy_reload')
        text = text.replace('-m runtime.resources', '-m native.resources')
        if name == 'watch_list.sh':
            text = adapt_mark_checksum(text)
        if name == 'data_update.sh':
            text = adapt_root_hints_update(text)
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    # Container-private listeners become loopback-only on the Debian host.
    dnscrypt = directory / 'dnscrypt.toml'
    dnscrypt.write_text(replace_once(dnscrypt.read_text(), "['0.0.0.0:5302']", "['127.0.0.1:5302']"))
