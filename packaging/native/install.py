#!/usr/bin/env python3
"""Install, upgrade and roll back one LovestDNS systemd instance on Debian."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

DNS_KEYS = {'TZ', 'CNAUTO', 'CNFALL', 'CN_TRACKER', 'USE_HOSTS', 'IPV6', 'SOCKS5', 'SERVER_IP',
            'CUSTOM_FORWARD', 'CUSTOM_FORWARD_TTL', 'AUTO_FORWARD', 'AUTO_FORWARD_CHECK', 'USE_MARK_DATA',
            'RULES_TTL', 'HTTP_FILE', 'QUERY_TIME', 'ADDINFO', 'SHUFFLE', 'EXPIRED_FLUSH', 'SAFEMODE', 'UPDATE',
            'DNS_SERVERNAME'}
DEFAULTS = {'WEB_UI_TOKEN': '', 'SUITE_READ_TOKEN': '', 'SUITE_INSTANCE_NAME': 'LovestDNS',
            'WEB_BIND_ADDRESS': '127.0.0.1', 'WEB_HOST_PORT': '8123', 'DNS_BIND_ADDRESS': '0.0.0.0',
            'SUITE_DNS_PORT': '53', 'FORWARD_BIND_ADDRESS': '127.0.0.1',
            'MEMORY_MAX_MB': '6144', 'CACHE_BUDGET_MB': '4096', 'TZ': 'Asia/Shanghai', 'UPDATE': 'weekly',
            'CNAUTO': 'yes', 'CNFALL': 'yes', 'CN_TRACKER': 'yes', 'USE_HOSTS': 'no', 'IPV6': 'no',
            'SOCKS5': 'IP:PORT', 'SERVER_IP': 'none', 'CUSTOM_FORWARD': 'IP:PORT', 'CUSTOM_FORWARD_TTL': '0',
            'AUTO_FORWARD': 'no', 'AUTO_FORWARD_CHECK': 'yes', 'USE_MARK_DATA': 'yes', 'RULES_TTL': '0',
            'HTTP_FILE': 'no', 'QUERY_TIME': '2000ms', 'ADDINFO': 'no', 'SHUFFLE': 'no', 'EXPIRED_FLUSH': 'yes',
            'DNS_SERVERNAME': 'LovestDNS'}


def command(args, *, check=True, capture=False, **kwargs):
    return subprocess.run(args, check=check, text=True, capture_output=capture, **kwargs)


def paths(instance):
    if instance and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', instance):
        raise ValueError('Invalid instance name')
    name = 'lovestdns' + ('-' + instance if instance else '')
    return {'name': name, 'home': Path('/opt') / name, 'storage': Path('/var/lib') / name,
            'config': Path('/etc') / name / (name + '.env'), 'run': Path('/run') / name,
            'unit': Path('/etc/systemd/system') / (name + '.service'),
            'ctl': Path('/usr/local/sbin') / (name + 'ctl')}


def read_env(path):
    result = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in set(DEFAULTS) | DNS_KEYS:
            raise ValueError(f'Unsupported environment key at line {number}')
        tokens = shlex.split(value, posix=True)
        if len(tokens) > 1:
            raise ValueError(f'Quote the environment value at line {number}')
        result[key] = tokens[0] if tokens else ''
    return result


def write_env(path, values):
    lines = []
    for key, value in sorted(values.items()):
        if any(c in value for c in '\r\n\x00'):
            raise ValueError('Environment values must occupy one line')
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'{key}="{value}"')
    atomic(path, '\n'.join(lines) + '\n', 0o600)


def atomic(path, text, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError('Refusing symbolic-link configuration files')
    with temporary.open('w') as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def validate_config(config):
    for key in ('DNS_BIND_ADDRESS', 'WEB_BIND_ADDRESS', 'FORWARD_BIND_ADDRESS'):
        ipaddress.ip_address(config[key])
    for key in ('SUITE_DNS_PORT', 'WEB_HOST_PORT'):
        if not config[key].isdigit() or not 1 <= int(config[key]) <= 65535:
            raise ValueError(f'Invalid {key}')
    if config['UPDATE'] not in {'no', 'daily', 'weekly', 'monthly'}:
        raise ValueError('Invalid UPDATE interval')
    for key, minimum in (('MEMORY_MAX_MB', 512), ('CACHE_BUDGET_MB', 32)):
        if not config[key].isdigit() or not minimum <= int(config[key]) <= 65536:
            raise ValueError(f'Invalid {key}')
    if not config['WEB_UI_TOKEN']:
        raise ValueError('WEB_UI_TOKEN is required')
    if config['SUITE_READ_TOKEN'] and secrets.compare_digest(config['WEB_UI_TOKEN'], config['SUITE_READ_TOKEN']):
        raise ValueError('Management and observer tokens must differ')


def verify_package(package):
    package = Path(package).resolve()
    manifest = json.loads((package / 'manifest.json').read_text())
    if manifest.get('product') != 'LovestDNS' or manifest.get('deployment') != 'native' or manifest.get('architecture') != 'amd64':
        raise ValueError('Unsupported package identity')
    version = manifest['version']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
        raise ValueError('Invalid package version')
    for name, digest in manifest['files'].items():
        path = package / name
        if path.is_symlink() or not path.is_file() or package not in path.resolve().parents:
            raise ValueError('Unsafe or missing package file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Package checksum differs: {name}')
    if any(p.is_symlink() for p in package.rglob('*')):
        raise ValueError('Package contains symbolic links')
    actual = {p.relative_to(package).as_posix() for p in package.rglob('*') if p.is_file() and p != package / 'manifest.json'}
    if actual != set(manifest['files']):
        raise ValueError('Package contains unexpected files')
    return manifest


def service_unit(p, config):
    current = p['home'] / 'current'
    state = p['storage'] / 'state'
    dns_probe = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(config['DNS_BIND_ADDRESS'], config['DNS_BIND_ADDRESS'])
    # PrivateTmp and a single /data alias preserve upstream binary interfaces.
    # Debian's OS, tools and Python remain the actual service environment.
    return f"""[Unit]
Description=LovestDNS native DNS and Web management
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=180
StartLimitBurst=3

[Service]
Type=simple
EnvironmentFile={p['config']}
Environment=LOVESTDNS_SYSTEMD=1
Environment=LOVESTDNS_HOME={current}
Environment=DATA_DIR=/data
Environment=SUITE_STATE_DIR={state}
Environment=SUITE_RUN_DIR={p['run']}
Environment=SUITE_DIST_DIR={current}/dist
Environment=SUITE_BUILD_INFO={current}/build-info.json
Environment=LOVESTDNS_UPDATE_DIR={p['storage']}/updates
Environment=DNS_TEST_PORT={config['SUITE_DNS_PORT']}
Environment=DNS_TEST_SERVER={dns_probe}
Environment=PYTHONPATH={current}/app/backend:{current}/app/base:{current}/app
Environment=PYTHONUNBUFFERED=1
Environment=PATH={current}/core:{current}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WorkingDirectory={current}/app
ExecStart={current}/venv/bin/python -m native.entrypoint
Restart=on-failure
RestartSec=5
TimeoutStopSec=150
KillMode=mixed
RuntimeDirectory={p['name']}
RuntimeDirectoryMode=0700
BindPaths={p['storage']}/data:/data
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/data {p['storage']} {p['run']}
InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock
NoNewPrivileges=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK AF_PACKET
LimitNOFILE=1048576
MemoryMax={config['MEMORY_MAX_MB']}M
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def is_active(p):
    return command(['systemctl', 'is-active', '--quiet', p['name'] + '.service'], check=False).returncode == 0


def stop(p):
    command(['systemctl', 'stop', p['name'] + '.service'])


def healthy(p, timeout=240):
    config = {**DEFAULTS, **read_env(p['config'])}
    host = config['WEB_BIND_ADDRESS']
    if host == '0.0.0.0':
        host = '127.0.0.1'
    elif host == '::':
        host = '::1'
    if ':' in host:
        host = '[' + host + ']'
    base = f"http://{host}:{config['WEB_HOST_PORT']}"
    # The host uses the private collector socket and DNS wire directly, so
    # readiness never requires enabling external token access.
    spec = importlib.util.spec_from_file_location('native_runtime_probe', p['home'] / 'current/app/backend/runtime_client.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    prior_run = os.environ.get('SUITE_RUN_DIR')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.environ['SUITE_RUN_DIR'] = str(p['run'])
            try:
                value = probe.snapshot()
            finally:
                if prior_run is None:
                    os.environ.pop('SUITE_RUN_DIR', None)
                else:
                    os.environ['SUITE_RUN_DIR'] = prior_run
            bootstrap = value.get('components', {}).get('bootstrap', {})
            if bootstrap.get('statename') == 'FATAL' or (bootstrap.get('statename') == 'EXITED' and bootstrap.get('exitstatus', 0)):
                raise RuntimeError('Native bootstrap failed; inspect the bootstrap component log')
            if value.get('core_healthy'):
                with opener.open(base + '/', timeout=5) as response:
                    if response.status != 200:
                        raise ValueError('Web page unavailable')
                target = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(config['DNS_BIND_ADDRESS'], config['DNS_BIND_ADDRESS'])
                query_id = secrets.token_bytes(2)
                name = b'lovestdns-ready'
                packet = query_id + bytes.fromhex('01000001000000000000') + bytes([len(name)]) + name + b'\x07invalid\x00\x00\x01\x00\x01'
                with socket.socket(socket.AF_INET6 if ':' in target else socket.AF_INET, socket.SOCK_DGRAM) as dns:
                    dns.settimeout(5)
                    dns.connect((target, int(config['SUITE_DNS_PORT'])))
                    dns.send(packet)
                    reply = dns.recv(65535)
                if len(reply) >= 12 and reply[:2] == query_id and reply[2] & 0x80:
                    return {'core_healthy': True, 'dns_responded': True, 'scope': 'component readiness and protocol response; not every rule or public domain'}
        except (OSError, ValueError, KeyError, urllib.error.HTTPError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Native service did not pass readiness and DNS response checks; inspect its component logs')
        time.sleep(3)


def switch(link, target):
    temporary = link.with_name(link.name + '.new')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('A pending release link already exists')
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def preflight_ports(config):
    ports = [(config['WEB_BIND_ADDRESS'], int(config['WEB_HOST_PORT']), socket.SOCK_STREAM),
             (config['DNS_BIND_ADDRESS'], int(config['SUITE_DNS_PORT']), socket.SOCK_STREAM),
             (config['DNS_BIND_ADDRESS'], int(config['SUITE_DNS_PORT']), socket.SOCK_DGRAM)]
    if config['CNAUTO'] != 'no':
        ports += [('127.0.0.1', port, kind) for port in (5301, 5302, 5303) for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM)]
        ports += [(config['FORWARD_BIND_ADDRESS'], 5304, kind) for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM)]
    for address, port, kind in ports:
        with socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, kind) as probe:
            try:
                probe.bind((address, port))
            except OSError as exc:
                raise ValueError(f'Port unavailable: {address}:{port}; no existing service was stopped') from exc


def dependencies():
    if platform.machine() not in {'x86_64', 'amd64'}:
        raise ValueError('This package is Linux amd64 only')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'debian' or release.get('VERSION_ID', '').strip('"') not in {'12', '13'}:
        raise ValueError('Debian 12 or 13 is required')
    required = ['python3-venv', 'inotify-tools', 'iproute2', 'procps', 'xz-utils', 'ca-certificates', 'bash']
    missing = []
    for package in required:
        status = command(['dpkg-query', '-W', '-f=${Status}', package], check=False, capture=True)
        if status.returncode or status.stdout.strip() != 'install ok installed':
            missing.append(package)
    if missing:
        command(['apt-get', 'update'])
        command(['apt-get', 'install', '-y', '--no-install-recommends', *missing],
                env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})


@contextmanager
def install_lock(p):
    import fcntl
    p['home'].mkdir(parents=True, exist_ok=True, mode=0o755)
    with (p['home'] / '.install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def installation_receipt(p, release, archive_sha256=None):
    """Keep the outer archive digest outside its own payload and bind it to this release."""
    manifest = (release / 'manifest.json').read_bytes()
    info = json.loads(manifest)
    if archive_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}', archive_sha256):
        raise ValueError('Invalid installation archive digest')
    receipts = p['home'] / 'receipts'
    receipts.mkdir(exist_ok=True, mode=0o700)
    receipt = {'format_version': 1, 'service_name': p['name'], 'release_path': str(release.resolve()),
               'version': info['version'], 'manifest_sha256': hashlib.sha256(manifest).hexdigest(),
               'archive_sha256': archive_sha256, 'installed_at': time.time()}
    atomic(receipts / (release.name + '.json'), json.dumps(receipt, sort_keys=True) + '\n')
    return receipt


def configure_online_updater(p, release):
    """The privileged installer owns the fixed worker units and protocol path."""
    root = p['storage'] / 'updates'
    path_unit = p['unit'].with_name(p['name'] + '-update.path')
    worker_unit = p['unit'].with_name(p['name'] + '-update.service')
    if not (release / 'app/native/update_worker.py').is_file():
        # A rollback to a release predating browser updates must not leave a
        # watcher invoking code that is no longer part of the active package.
        command(['systemctl', 'disable', '--now', path_unit.name], check=False, capture=True)
        (root / 'protocol.json').unlink(missing_ok=True)
        return
    root.mkdir(exist_ok=True, mode=0o700)
    instance = p['name'].removeprefix('lovestdns').removeprefix('-')
    invocation = f'{release}/venv/bin/python -m native.update_worker --instance {shlex.quote(instance)}'
    atomic(worker_unit, f"""[Unit]
Description=LovestDNS verified software update ({p['name']})
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=oneshot
Environment=PYTHONPATH={release}/app
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={invocation}
ExecStopPost={invocation} --recover
TimeoutStartSec=1200
TimeoutStopSec=300
KillMode=mixed
UMask=0077
PrivateTmp=true
PrivateDevices=true
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths={p['home']} {p['storage']} {p['config'].parent} /etc/systemd/system /usr/local/sbin
NoNewPrivileges=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
MemoryMax=768M
CPUQuota=100%
""", 0o644)
    atomic(path_unit, f"""[Unit]
Description=LovestDNS software update requests ({p['name']})

[Path]
PathExists={root}/request.json
Unit={worker_unit.name}

[Install]
WantedBy=multi-user.target
""", 0o644)
    atomic(root / 'protocol.json', json.dumps({'schema_version': 1, 'service_name': worker_unit.name}))
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'enable', '--now', path_unit.name])
    command(['systemctl', 'is-active', '--quiet', path_unit.name])


def install(args, package):
    manifest = verify_package(package)
    p = paths(args.instance)
    dependencies()
    with install_lock(p):
        release = p['home'] / 'releases' / manifest['version']
        if release.exists():
            current = p['home'] / 'current'
            if current.exists() and json.loads((current / 'manifest.json').read_text())['version'] == manifest['version']:
                raise ValueError('This version is already active; use the installed control command')
            # Retrying after a failed upgrade or explicit rollback must not
            # overwrite an earlier release or its venv.
            release = release.with_name(release.name + '-retry-' + secrets.token_hex(4))
        p['config'].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p['storage'].mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in ('data', 'state', 'backups'):
            (p['storage'] / child).mkdir(exist_ok=True, mode=0o700)
        values = {**DEFAULTS, 'WEB_UI_TOKEN': secrets.token_hex(32)}
        if p['config'].exists():
            values.update(read_env(p['config']))
        if args.config:
            values.update(read_env(args.config))
        validate_config(values)
        old = (p['home'] / 'current').resolve() if (p['home'] / 'current').exists() else None
        was_active = is_active(p)
        if not was_active:
            preflight_ports(values)
        shutil.copytree(package, release)
        # Build the new venv before stopping an existing DNS service.
        command([sys.executable, '-m', 'venv', str(release / 'venv')])
        command([str(release / 'venv/bin/python'), '-m', 'pip', 'install', '--no-index', '--find-links', str(release / 'wheels'),
                 '-r', str(release / 'requirements.lock.txt')])
        for name, arguments in (('redis-server', ['--version']), ('unbound', ['-V']), ('mosdns', ['version']), ('dnscrypt-proxy', ['-version'])):
            command([str(release / 'core' / name), *arguments], capture=True)
        installation_receipt(p, release, getattr(args, 'sha256', None))
        backup = None
        changed = False
        try:
            if old:
                size = sum(f.stat().st_size for name in ('data', 'state')
                           for f in (p['storage'] / name).rglob('*') if f.is_file())
                if shutil.disk_usage(p['storage']).free < size * 2 + 256 * 1024 * 1024:
                    raise ValueError('Not enough space for a consistent rollback backup')
                if was_active:
                    stop(p)
                candidate_backup = p['storage'] / 'backups' / time.strftime('%Y%m%d-%H%M%S')
                candidate_backup.mkdir(mode=0o700)
                for name in ('data', 'state'):
                    shutil.copytree(p['storage'] / name, candidate_backup / name, symlinks=True)
                shutil.copyfile(p['config'], candidate_backup / 'service.env')
                shutil.copyfile(p['unit'], candidate_backup / 'service.unit')
                atomic(candidate_backup / 'release.json', json.dumps({'release': str(old), 'was_active': was_active}))
                backup = candidate_backup
            elif args.import_data:
                source = Path(args.import_data).resolve(strict=True)
                if not source.is_dir() or any((p['storage'] / 'data').iterdir()):
                    raise ValueError('Import requires a directory and an empty target data directory')
                shutil.copytree(source, p['storage'] / 'data', dirs_exist_ok=True, symlinks=True)
            changed = True
            write_env(p['config'], values)
            atomic(p['unit'], service_unit(p, values), 0o644)
            switch(p['home'] / 'current', release)
            command(['systemctl', 'daemon-reload'])
            command(['systemctl', 'reset-failed', p['name'] + '.service'], check=False, capture=True)
            command(['systemctl', 'enable', '--now', p['name'] + '.service'])
            result = healthy(p)
            configure_online_updater(p, release)
            if backup:
                atomic(p['home'] / 'rollback.json', json.dumps({'backup': str(backup)}))
            ctl = '#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(p['home'] / 'current/install.py')) + ' --instance ' + shlex.quote(args.instance) + ' "$@"\n'
            atomic(p['ctl'], ctl, 0o755)
            print(json.dumps({'installed': manifest['version'], 'service': p['name'], 'config_file': str(p['config']),
                              'control': str(p['ctl']), 'rollback_backup': str(backup) if backup else None, **result}))
        except BaseException:
            if old:
                if backup and changed:
                    command(['systemctl', 'stop', p['name'] + '.service'], check=False, capture=True)
                    restore(p, backup)
                if was_active:
                    command(['systemctl', 'start', p['name'] + '.service'])
                    healthy(p)
                print('Previous release and its data/configuration restored.', file=sys.stderr)
            else:
                command(['systemctl', 'disable', '--now', p['name'] + '.service'], check=False, capture=True)
            raise


def restore(p, backup):
    info = json.loads((backup / 'release.json').read_text())
    old = Path(info['release']).resolve(strict=True)
    if old.parent != (p['home'] / 'releases').resolve():
        raise ValueError('Rollback release is outside this instance')
    failed = p['storage'] / 'backups' / ('pre-rollback-' + time.strftime('%Y%m%d-%H%M%S'))
    failed.mkdir(mode=0o700)
    # Preserve the displaced data for review; never delete it during rollback.
    for name in ('data', 'state'):
        (p['storage'] / name).rename(failed / name)
        shutil.copytree(backup / name, p['storage'] / name, symlinks=True)
    shutil.copyfile(backup / 'service.env', p['config'])
    shutil.copyfile(backup / 'service.unit', p['unit'])
    switch(p['home'] / 'current', old)
    configure_online_updater(p, old)
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'reset-failed', p['name'] + '.service'], check=False, capture=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', default='')
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('install', 'upgrade'):
        mode = commands.add_parser(name)
        mode.add_argument('--config', type=Path)
        mode.add_argument('--import-data', type=Path)
        if name == 'upgrade':
            mode.add_argument('archive', type=Path)
            mode.add_argument('--sha256', required=True)
    for name in ('status', 'health', 'start', 'stop', 'restart', 'logs', 'rollback', 'password'):
        commands.add_parser(name)
    commands.add_parser('api').add_argument('mode', choices=['on', 'off', 'status'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root or through sudo')
    p = paths(args.instance)
    if args.action == 'install':
        install(args, Path(__file__).resolve().parent)
    elif args.action == 'upgrade':
        if not re.fullmatch(r'[a-f0-9]{64}', args.sha256) or hashlib.sha256(args.archive.read_bytes()).hexdigest() != args.sha256:
            raise ValueError('Upgrade archive SHA-256 differs')
        with tempfile.TemporaryDirectory(prefix='lovestdns-upgrade-') as directory:
            root = Path(directory)
            with tarfile.open(args.archive) as archive:
                for member in archive.getmembers():
                    path = root / member.name
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()) or root not in path.resolve().parents:
                        raise ValueError('Unsafe archive entry')
                archive.extractall(root)
            packages = list(root.glob('*/manifest.json'))
            if len(packages) != 1:
                raise ValueError('Expected one native package')
            install(args, packages[0].parent)
    elif args.action == 'health':
        print(json.dumps(healthy(p, timeout=10)))
    elif args.action == 'rollback':
        with install_lock(p):
            backup = Path(json.loads((p['home'] / 'rollback.json').read_text())['backup']).resolve(strict=True)
            if backup.parent != (p['storage'] / 'backups').resolve():
                raise ValueError('Invalid rollback path')
            stop(p)
            restore(p, backup)
            command(['systemctl', 'start', p['name'] + '.service'])
            print(json.dumps(healthy(p)))
    elif args.action == 'logs':
        command(['journalctl', '-u', p['name'] + '.service', '-n', '80', '--no-pager'])
    elif args.action in {'password', 'api'}:
        current = p['home'] / 'current'
        options = ['--external-api', args.mode] if args.action == 'api' else []
        command([str(current / 'venv/bin/python'), str(current / 'app/backend/browser_auth.py'),
                 '--state-dir', str(p['storage'] / 'state'), *options])
    else:
        if args.action in {'start', 'restart'}:
            command(['systemctl', 'reset-failed', p['name'] + '.service'], check=False, capture=True)
        command(['systemctl', args.action, p['name'] + '.service'])
        if args.action in {'start', 'restart'}:
            print(json.dumps(healthy(p)))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
