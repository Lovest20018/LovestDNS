from contextlib import contextmanager, nullcontext
import hashlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('native_install', ROOT / 'packaging/native/install.py')
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


def package(tmp_path):
    path = tmp_path / 'package'
    path.mkdir()
    (path / 'payload').write_text('expected bytes')
    manifest = {'product': 'LovestDNS', 'deployment': 'native', 'architecture': 'amd64', 'version': 'test.1',
                'files': {'payload': hashlib.sha256((path / 'payload').read_bytes()).hexdigest()}}
    (path / 'manifest.json').write_text(json.dumps(manifest))
    return path


def test_native_payload_rejects_tamper_and_unlisted_files(tmp_path):
    path = package(tmp_path)
    assert install.verify_package(path)['version'] == 'test.1'
    (path / 'payload').write_text('modified')
    with pytest.raises(ValueError, match='checksum'):
        install.verify_package(path)
    (path / 'payload').write_text('expected bytes')
    (path / 'unexpected').write_text('not declared')
    with pytest.raises(ValueError, match='unexpected'):
        install.verify_package(path)


def upgrade_archive(tmp_path, kind=None):
    package_dir = package(tmp_path)
    archive_path = tmp_path / 'package.tar.gz'
    root = 'lovestdns-test.1' if kind != 'root-name' else 'other'

    def add_member(archive, name, content=b'', *, mode=0o644, member_type=tarfile.REGTYPE):
        member = tarfile.TarInfo(name)
        member.size = len(content)
        member.mode = mode
        member.type = member_type
        archive.addfile(member, BytesIO(content))

    with tarfile.open(archive_path, 'w:gz') as archive:
        for file in sorted(package_dir.iterdir()):
            add_member(archive, f'{root}/{file.name}', file.read_bytes())
        if kind == 'members':
            for index in range(4095):
                add_member(archive, f'{root}/file-{index}')
        elif kind == 'setuid':
            add_member(archive, f'{root}/program', mode=0o4755)
        elif kind == 'symlink':
            add_member(archive, f'{root}/link', member_type=tarfile.SYMTYPE)
        elif kind in {'parent', 'absolute', 'backslash', 'colon', 'duplicate', 'second-root'}:
            names = {'parent': f'{root}/child/../payload', 'absolute': f'/{root}/extra',
                     'backslash': f'{root}/a\\b', 'colon': f'{root}/a:b', 'duplicate': f'{root}//./payload',
                     'second-root': 'lovestdns-second/other'}
            add_member(archive, names[kind])
    return archive_path


def run_upgrade(monkeypatch, archive, digest):
    monkeypatch.setattr(install, 'os', SimpleNamespace(geteuid=lambda: 0, fstat=os.fstat))
    monkeypatch.setattr(sys, 'argv', ['install.py', 'upgrade', str(archive), '--sha256', digest])
    install.main()


@pytest.mark.parametrize('kind,error', [
    ('archive-size', 'package exceeds its size limit'), ('expanded-size', 'Extracted package exceeds'),
    ('members', 'Too many native package entries'), ('setuid', 'Unsafe native package entry'),
    ('parent', 'Unsafe native package entry'), ('absolute', 'Unsafe native package entry'),
    ('backslash', 'Unsafe native package entry'), ('colon', 'Unsafe native package entry'),
    ('duplicate', 'Unsafe native package entry'), ('symlink', 'Unsafe native package entry'),
    ('second-root', 'Expected one native package directory'), ('root-name', 'Expected one native package directory'),
    ('digest', 'SHA-256 differs'),
])
def test_upgrade_rejects_invalid_archive_before_extraction(tmp_path, monkeypatch, kind, error):
    archive = upgrade_archive(tmp_path, kind)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if kind == 'archive-size':
        monkeypatch.setattr(install, 'MAX_ARCHIVE', archive.stat().st_size - 1)
    elif kind == 'expanded-size':
        monkeypatch.setattr(install, 'MAX_EXTRACTED', 1)
    elif kind == 'digest':
        digest = '0' * 64
    monkeypatch.setattr(tarfile.TarFile, 'extractall', lambda *_args, **_kwargs: pytest.fail('Extracted an invalid archive'))
    monkeypatch.setattr(install, 'install', lambda *_: pytest.fail('Started installation from an invalid archive'))
    with pytest.raises(ValueError, match=error):
        run_upgrade(monkeypatch, archive, digest)


@pytest.mark.parametrize('with_data_filter', [True, False])
def test_upgrade_streams_checksums_and_uses_supported_extraction_filter(tmp_path, monkeypatch, with_data_filter):
    archive = upgrade_archive(tmp_path)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    read_bytes, extract = Path.read_bytes, tarfile.TarFile.extractall
    installed, filters = [], []

    def forbid_whole_file_reads(path):
        assert path != archive and path.name != 'payload'
        return read_bytes(path)

    def observe_extraction(archive, *args, **kwargs):
        filters.append(kwargs.get('filter'))
        return extract(archive, *args, **kwargs)

    def receive(args, path):
        assert args.sha256 == digest
        installed.append(install.verify_package(path)['version'])

    if not with_data_filter:
        monkeypatch.delattr(tarfile, 'data_filter', raising=False)
    monkeypatch.setattr(Path, 'read_bytes', forbid_whole_file_reads)
    monkeypatch.setattr(tarfile.TarFile, 'extractall', observe_extraction)
    monkeypatch.setattr(install, 'install', receive)
    run_upgrade(monkeypatch, archive, digest)
    assert installed == ['test.1']
    assert filters == ['data' if with_data_filter and hasattr(tarfile, 'data_filter') else None]


def test_atomic_uses_private_unique_files_and_cleans_after_failed_replace(tmp_path, monkeypatch):
    path = tmp_path / 'service.env'
    path.write_text('original')
    seen = []
    replace = os.replace

    def track(source, target):
        temporary = Path(source)
        assert temporary.parent == path.parent
        if os.name == 'posix':
            assert temporary.stat().st_mode & 0o777 == 0o640
        seen.append(temporary)
        if len(seen) == 1:
            raise OSError('injected replace failure')
        replace(source, target)

    monkeypatch.setattr(os, 'replace', track)
    with pytest.raises(OSError, match='injected'):
        install.atomic(path, 'failed', 0o640)
    assert path.read_text() == 'original'
    assert list(tmp_path.iterdir()) == [path]
    install.atomic(path, 'saved', 0o640)
    assert path.read_text() == 'saved' and seen[0] != seen[1]


def test_atomic_refuses_symbolic_links(tmp_path):
    target = tmp_path / 'target'
    target.write_text('original')
    path = tmp_path / 'service.env'
    try:
        path.symlink_to(target)
    except OSError:
        if os.name == 'nt':
            pytest.skip('file symlinks unavailable')
        raise
    with pytest.raises(ValueError, match='symbolic-link'):
        install.atomic(path, 'changed')
    assert path.is_symlink() and target.read_text() == 'original'


def test_preflight_enables_reuse_only_for_tcp(monkeypatch):
    probes = []

    class Socket:
        def __init__(self, family, kind):
            self.kind = kind
            self.options = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, level, option, value):
            self.options.append((level, option, value))

        def bind(self, address):
            probes.append((self.kind, address, self.options))

    monkeypatch.setattr(install.socket, 'socket', Socket)
    install.preflight_ports({**install.DEFAULTS, 'CNAUTO': 'no'})
    reuse = [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]
    assert probes == [(socket.SOCK_STREAM, ('127.0.0.1', 8123), reuse),
                      (socket.SOCK_STREAM, ('0.0.0.0', 53), reuse), (socket.SOCK_DGRAM, ('0.0.0.0', 53), [])]


def test_native_health_uses_private_collector_and_dns_without_token_api(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    backend = home / 'current/app/backend'
    backend.mkdir(parents=True)
    (backend / 'runtime_client.py').write_text('def snapshot():\n    return {"core_healthy": True}\n')
    monkeypatch.setattr(install, 'read_env', lambda _: {**install.DEFAULTS, 'WEB_UI_TOKEN': 'unused-test-token'})
    monkeypatch.setenv('SUITE_RUN_DIR', 'original-test-run')
    calls = []
    class Web:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Opener:
        def open(self, url, timeout):
            calls.append(url)
            return Web()
    class DNS:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, _): pass
        def connect(self, target): assert target == ('127.0.0.1', 53)
        def send(self, packet): self.packet = packet
        def recv(self, _): return self.packet[:2] + b'\x81\x83' + self.packet[4:]
    monkeypatch.setattr(install.urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(install.socket, 'socket', lambda *_: DNS())
    p = {'home': home, 'run': tmp_path / 'run', 'config': tmp_path / 'config'}
    assert install.healthy(p)['dns_responded']
    assert calls == ['http://127.0.0.1:8123/']
    assert install.os.environ['SUITE_RUN_DIR'] == 'original-test-run'


def test_native_health_rejects_failed_bootstrap_before_dns_probe(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    backend = home / 'current/app/backend'
    backend.mkdir(parents=True)
    (backend / 'runtime_client.py').write_text('def snapshot():\n    return {"components": {"bootstrap": {"statename": "FATAL"}}}\n')
    monkeypatch.setattr(install, 'read_env', lambda _: install.DEFAULTS.copy())
    with pytest.raises(RuntimeError, match='bootstrap failed'):
        install.healthy({'home': home, 'run': tmp_path / 'run', 'config': tmp_path / 'config'}, timeout=0)


def test_native_environment_is_literal_and_rejects_runtime_override(tmp_path):
    path = tmp_path / 'native.env'
    values = {'WEB_UI_TOKEN': 'literal-$(id)-`id`-$HOME-"quoted"', 'SOCKS5': '127.0.0.1:1080'}
    install.write_env(path, values)
    assert install.read_env(path) == values
    path.write_text('PYTHONPATH="/data/evil"\n')
    with pytest.raises(ValueError, match='Unsupported'):
        install.read_env(path)
    with pytest.raises(ValueError, match='one line'):
        install.write_env(path, {'WEB_UI_TOKEN': 'token\nInjected=yes'})


def test_install_receipts_live_outside_packages_and_survive_release_selection(tmp_path):
    p = {'name': 'lovestdns-validation', 'home': tmp_path}
    release1 = tmp_path / 'releases' / 'test.1'
    release1.mkdir(parents=True)
    (release1 / 'manifest.json').write_text(json.dumps({'version': 'test.1'}))
    receipt1 = install.installation_receipt(p, release1)
    assert receipt1['archive_sha256'] is None
    assert list(release1.iterdir()) == [release1 / 'manifest.json']
    release2 = tmp_path / 'releases' / 'test.2'
    release2.mkdir()
    (release2 / 'manifest.json').write_text(json.dumps({'version': 'test.2'}))
    receipt2 = install.installation_receipt(p, release2, 'b' * 64)
    assert receipt2['archive_sha256'] == 'b' * 64
    assert receipt1['manifest_sha256'] != receipt2['manifest_sha256']
    assert json.loads((tmp_path / 'receipts/test.1.json').read_text()) == receipt1
    assert json.loads((tmp_path / 'receipts/test.2.json').read_text()) == receipt2


def resolve_selected_release(monkeypatch, current, selected):
    # Keep current's symlink semantics in tests without Windows symlink privileges.
    resolve = Path.resolve

    def resolve_link(path, *args, **kwargs):
        if path == current and selected:
            path = selected[-1]
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve_link)


@pytest.fixture
def prune_instance(tmp_path, monkeypatch):
    p = {key: tmp_path / key for key in ('home', 'storage', 'config', 'unit', 'run', 'ctl')}
    p['name'] = 'lovestdns-test'
    for root in (p['home'] / 'releases', p['home'] / 'receipts', p['storage'] / 'backups'):
        root.mkdir(parents=True)

    # Simulate the install lock on Windows and assert that install and prune
    # never try to acquire the non-reentrant lock twice.
    held = False

    @contextmanager
    def exclusive(paths):
        nonlocal held
        assert paths == p and not held
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(install, 'install_lock', exclusive)
    return p


@pytest.fixture
def install_link(monkeypatch):
    aliases = {}
    resolve, path_stat = Path.resolve, Path.stat

    def link(path, target, *, is_dir=False):
        try:
            path.symlink_to(target, target_is_directory=is_dir)
        except OSError as exc:
            if os.name != 'nt' or exc.winerror != 1314:
                raise
            # These Windows runners lack symlink privileges. Keep the same
            # symlink metadata and resolution at the filesystem boundary.
            path.touch()
            aliases[path] = target

    def resolve_link(path, *args, **kwargs):
        return resolve(aliases.get(path, path), *args, **kwargs)

    def stat_link(path, *, follow_symlinks=True):
        if path in aliases:
            if not follow_symlinks:
                return SimpleNamespace(st_mode=stat.S_IFLNK)
            path = aliases[path]
        return path_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, 'resolve', resolve_link)
    monkeypatch.setattr(Path, 'stat', stat_link)
    return link


def older_directory(path, age_order, content=b''):
    path.mkdir()
    (path / 'payload').write_bytes(content)
    stamp = 1_700_000_000_000_000_000 + age_order * 100_000
    os.utime(path, ns=(stamp, stamp))
    return path


def test_prune_cli_preview_and_actual_removal_protect_referenced_paths(prune_instance, install_link, monkeypatch, capsys):
    p = prune_instance
    releases, receipts, backups = p['home'] / 'releases', p['home'] / 'receipts', p['storage'] / 'backups'
    active = older_directory(releases / 'active', 0)
    rollback = older_directory(releases / 'older-rollback', 1)
    retry = older_directory(releases / 'old-retry-a1b2', 2, b'retry')
    kept_release = older_directory(releases / 'newer-unused', 3)
    protected = older_directory(backups / '20261001-120000', 0)
    record = protected / 'release.json'
    record.write_text(json.dumps({'release': str(rollback)}))
    os.utime(protected, ns=(active.stat().st_mtime_ns, active.stat().st_mtime_ns))
    old_backup = older_directory(backups / '20261003-120000', 2, b'backup')
    displaced = older_directory(backups / 'pre-rollback-a3b1', 3, b'displaced')
    kept_backup = older_directory(backups / '20261002-120000-abcd', 4)
    unknown = older_directory(backups / 'foreign-backup', 1, b'preserve')
    outside = older_directory(p['home'] / 'outside', 2, b'outside')
    (backups / '20261002-220000').touch()
    (releases / 'non-release-file').touch()
    install_link(p['home'] / 'current', active, is_dir=True)
    install_link(backups / '20261004-120000', outside, is_dir=True)
    install_link(releases / 'external-link', outside, is_dir=True)
    displaced_mtime = displaced.stat().st_mtime_ns
    install_link(displaced / 'nested-link', outside, is_dir=True)
    os.utime(displaced, ns=(displaced_mtime, displaced_mtime))
    (p['home'] / 'rollback.json').write_text(json.dumps({'backup': str(protected)}))
    for name in (active.name, rollback.name, kept_release.name, retry.name, 'external-link', 'orphan'):
        (receipts / (name + '.json')).write_bytes(b'{}')
    install_link(receipts / 'linked.json', receipts / 'orphan.json')
    (receipts / 'directory.json').mkdir()
    (receipts / 'foreign.txt').touch()

    monkeypatch.setattr(install.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(install, 'paths', lambda instance: p)
    monkeypatch.setattr(sys, 'argv', ['lovestdnsctl', 'prune', '--keep-backups', '1', '--keep-releases', '1', '--dry-run'])
    install.main()
    preview = json.loads(capsys.readouterr().out)
    assert preview == {'dry_run': True, 'protected_backups': [str(protected)], 'kept_backups': [str(kept_backup)],
                       'removed_backups': [str(displaced), str(old_backup)],
                       'protected_releases': [str(active), str(rollback)], 'kept_releases': [str(kept_release)],
                       'removed_releases': [str(retry)],
                       'removed_receipts': [str(receipts / f'{name}.json') for name in ('external-link', retry.name, 'orphan')],
                       'reclaimed_bytes': len(b'displacedbackupretry{}{}{}')}
    for key in ('removed_backups', 'removed_releases', 'removed_receipts'):
        assert all(Path(name).exists() for name in preview[key])

    assert install.prune(p, keep_backups=1, keep_releases=1) == {**preview, 'dry_run': False}
    for key in ('removed_backups', 'removed_releases', 'removed_receipts'):
        assert not any(Path(name).exists() for name in preview[key])
    assert unknown.is_dir() and (outside / 'payload').read_bytes() == b'outside'
    assert (p['home'] / 'current').resolve(strict=True) == active and record.is_file()
    assert (backups / '20261004-120000').is_symlink() and (releases / 'external-link').is_symlink()
    assert (backups / '20261002-220000').is_file() and (releases / 'non-release-file').is_file()
    assert (receipts / 'linked.json').is_symlink() and (receipts / 'directory.json').is_dir()
    assert (receipts / 'foreign.txt').is_file()
    assert all((receipts / (release.name + '.json')).is_file() for release in (active, rollback, kept_release))
    assert not install.prune(p, keep_backups=1, keep_releases=1)['reclaimed_bytes']


def test_prune_refuses_a_redirected_release_root(prune_instance, install_link, tmp_path):
    p = prune_instance
    releases = p['home'] / 'releases'
    releases.rmdir()
    outside = tmp_path / 'other-instance'
    outside.mkdir()
    release = older_directory(outside / 'release', 0, b'outside')
    backup = older_directory(p['storage'] / 'backups/20261001-120000', 0)
    install_link(releases, outside, is_dir=True)
    with pytest.raises(ValueError, match='Unsafe releases directory'):
        install.prune(p, keep_backups=0, keep_releases=0)
    assert release.is_dir() and backup.is_dir()


@pytest.mark.parametrize('broken', ['invalid-json', 'unreadable', 'wrong-type', 'outside-backup', 'bad-release'])
def test_prune_rejects_invalid_rollback_metadata_before_any_removal(prune_instance, tmp_path, monkeypatch, broken):
    p = prune_instance
    release = older_directory(p['home'] / 'releases/old', 0, b'release')
    backup = older_directory(p['storage'] / 'backups/20261001-120000', 0, b'backup')
    receipt = p['home'] / 'receipts/orphan.json'
    receipt.touch()
    rollback = p['home'] / 'rollback.json'
    rollback.write_text(json.dumps({'backup': str(backup)}))
    (backup / 'release.json').write_text(json.dumps({'release': str(release)}))
    if broken == 'invalid-json':
        rollback.write_text('{')
    elif broken == 'unreadable':
        read = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == rollback:
                raise PermissionError('injected unreadable rollback.json')
            return read(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', read_text)
    elif broken == 'wrong-type':
        rollback.write_text(json.dumps({'backup': []}))
    elif broken == 'outside-backup':
        rollback.write_text(json.dumps({'backup': str(tmp_path)}))
    else:
        (backup / 'release.json').write_text(json.dumps({'release': str(tmp_path)}))

    with pytest.raises(ValueError, match='invalid rollback.json or release.json'):
        install.prune(p, keep_backups=0, keep_releases=0)
    assert all(path.exists() for path in (release, backup, receipt))


@pytest.mark.parametrize('value', ['-1', '101', '1.5'])
def test_prune_cli_rejects_invalid_retention_counts(value, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['lovestdnsctl', 'prune', '--keep-backups', value])
    with pytest.raises(SystemExit) as error:
        install.main()
    assert error.value.code == 2 and '--keep-backups' in capsys.readouterr().err


@pytest.mark.parametrize('prune_error', [None, OSError, ValueError])
def test_same_second_upgrades_reserve_unique_backups_and_prune_after_commit(prune_instance, tmp_path, monkeypatch,
                                                                          capsys, prune_error):
    p = prune_instance
    package_dir = package(tmp_path)
    releases, backups = p['home'] / 'releases', p['storage'] / 'backups'
    active = older_directory(releases / 'old', 0)
    (active / 'manifest.json').write_text(json.dumps({'version': 'old'}))
    old_backups = [older_directory(backups / f'20261001-12000{i}', i) for i in range(3)]
    old_releases = [older_directory(releases / f'old-retry-{i}', i) for i in range(3)]
    install.write_env(p['config'], {**install.DEFAULTS, 'WEB_UI_TOKEN': 'local-test-token'})
    p['unit'].write_text('old unit')
    current = p['home'] / 'current'
    current.mkdir()
    selected, pruned, suffix_sizes = [active], [], []
    resolve_selected_release(monkeypatch, current, selected)
    suffixes = iter(['1a2b', '1a2b', '3c4d'])
    token_hex, prune, lock = install.secrets.token_hex, install._prune_unlocked, install.install_lock
    lock_events = []

    @contextmanager
    def tracked_lock(paths):
        with lock(paths):
            lock_events.append('acquired')
            try:
                yield
            finally:
                lock_events.append('released')

    def token(size):
        if size == 2:
            suffix_sizes.append(size)
            return next(suffixes)
        return token_hex(size)

    def prune_after_upgrade(paths, **options):
        lock_events.append('pruned')
        assert options == {'keep_backups': 2, 'keep_releases': 2}
        assert p['ctl'].is_file() and current.resolve() == selected[-1]
        backup = Path(json.loads((p['home'] / 'rollback.json').read_text())['backup'])
        assert json.loads((backup / 'release.json').read_text())['release'] == str(selected[-2])
        pruned.append(backup)
        if prune_error:
            raise prune_error('injected prune failure')
        return prune(paths, **options)

    def unexpected_restore(*args, **kwargs):
        pytest.fail('A cleanup error rolled back a successful install')

    for name, substitute in {
        'paths': lambda _: p, 'dependencies': lambda: None, 'is_active': lambda _: True,
        'command': lambda *args, **kwargs: None, 'healthy': lambda _: {'dns_responded': True},
        'switch': lambda path, release: selected.append(release), 'configure_online_updater': lambda *_: None,
        'restore': unexpected_restore, 'install_lock': tracked_lock, '_prune_unlocked': prune_after_upgrade,
        'time': SimpleNamespace(strftime=lambda _: '20261127-120000', time=time.time),
    }.items():
        monkeypatch.setattr(install, name, substitute)
    monkeypatch.setattr(install.secrets, 'token_hex', token)
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024 ** 3))
    args = SimpleNamespace(instance='test', config=None, import_data=None)
    for expected in ('test.1', 'test.2'):
        manifest = package_dir / 'manifest.json'
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), 'version': expected}))
        install.install(args, package_dir)
        captured = capsys.readouterr()
        assert json.loads(captured.out)['installed'] == expected
        assert ('injected prune failure' in captured.err) == bool(prune_error)
        assert selected[-1].name == expected

    assert lock_events == ['acquired', 'pruned', 'released'] * 2
    assert suffix_sizes == [2, 2, 2]
    assert [path.name for path in pruned] == ['20261127-120000-1a2b', '20261127-120000-3c4d']
    assert all(path.is_dir() for path in pruned)
    assert old_backups[0].is_dir() == old_releases[0].is_dir() == bool(prune_error)


@pytest.mark.parametrize('failure', ['copy', 'venv', 'self-check', 'receipt', 'readiness'])
def test_failed_install_cleans_unselected_release_and_keeps_old(tmp_path, monkeypatch, failure):
    package_dir = package(tmp_path)
    p = {key: tmp_path / key for key in ('home', 'storage', 'config', 'unit', 'run', 'ctl')}
    p['name'] = 'lovestdns-test'
    old = p['home'] / 'releases/old'
    old.mkdir(parents=True)
    (old / 'manifest.json').write_text(json.dumps({'version': 'old'}))
    (old / 'payload').write_text('active data')
    install.installation_receipt(p, old)
    receipt = p['home'] / 'receipts/old.json'
    original_receipt = receipt.read_bytes()
    install.write_env(p['config'], {**install.DEFAULTS, 'WEB_UI_TOKEN': 'local-test-token'})
    p['unit'].write_text('old service unit')
    current = p['home'] / 'current'
    shutil.copytree(old, current)
    selected, commands = [old], []
    resolve_selected_release(monkeypatch, current, selected)
    copytree, write_receipt = shutil.copytree, install.installation_receipt

    def copy(source, target, *args, **kwargs):
        if source == package_dir and failure == 'copy':
            (target / 'partial').touch()
            raise OSError('injected copy failure')
        return copytree(source, target, *args, **kwargs)

    def command(argv, **_):
        commands.append(argv)
        if (failure == 'venv' and argv[1:3] == ['-m', 'venv']
                or failure == 'self-check' and Path(argv[0]).name == 'redis-server'):
            raise RuntimeError(f'injected {failure} failure')

    def record_receipt(*args):
        write_receipt(*args)
        if failure == 'receipt':
            raise OSError('injected receipt failure')

    def switch(link, release):
        assert link == current
        selected.append(release)

    def healthy(_):
        if selected[-1] != old:
            raise RuntimeError('injected readiness failure')
        return {'dns_responded': True}

    for name, substitute in {
        'paths': lambda _: p, 'dependencies': lambda: None, 'install_lock': lambda _: nullcontext(),
        'is_active': lambda _: True, 'command': command, 'switch': switch, 'healthy': healthy,
        'installation_receipt': record_receipt, 'configure_online_updater': lambda *_: None,
    }.items():
        monkeypatch.setattr(install, name, substitute)
    monkeypatch.setattr(shutil, 'copytree', copy)
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024 ** 3))

    args = SimpleNamespace(instance='test', config=None, import_data=None)
    with pytest.raises((OSError, RuntimeError), match=f'injected {failure}'):
        install.install(args, package_dir)
    assert current.resolve() == old
    assert list(old.parent.iterdir()) == [old]
    assert list(receipt.parent.iterdir()) == [receipt]
    assert receipt.read_bytes() == original_receipt
    assert (old / 'payload').read_text() == 'active data'
    assert p['unit'].read_text() == 'old service unit'
    if failure == 'readiness':
        assert selected == [old, old.with_name('test.1'), old]
    else:
        assert selected == [old]
        assert all(argv[0] != 'systemctl' for argv in commands)


@pytest.mark.parametrize('failure', ['readiness', 'updater'])
def test_failed_first_install_can_retry_same_version(tmp_path, monkeypatch, failure):
    package_dir = package(tmp_path)
    source = tmp_path / 'import'
    source.mkdir()
    (source / 'rules').write_text('original imported rules')
    unit = tmp_path / 'systemd/lovestdns-test.service'
    unit.parent.mkdir()
    paths = {'name': 'lovestdns-test', 'home': tmp_path / 'home', 'storage': tmp_path / 'storage',
             'config': tmp_path / 'etc/service.env', 'unit': unit, 'run': tmp_path / 'run',
             'ctl': tmp_path / 'lovestdns-testctl'}
    paths['home'].mkdir()
    args = SimpleNamespace(instance='test', config=None, import_data=source)
    calls, selected = [], []
    active = False
    resolve_selected_release(monkeypatch, paths['home'] / 'current', selected)

    def command(argv, **_):
        nonlocal active
        calls.append(argv)
        if argv == ['systemctl', 'enable', '--now', unit.name]:
            active = True
        elif argv == ['systemctl', 'disable', '--now', unit.name]:
            active = False

    def switch(current, release):
        selected.append(release)
        shutil.copytree(release, current, dirs_exist_ok=True)

    def probe(_):
        if failure == 'readiness' and len(selected) == 1:
            raise RuntimeError(failure)
        return {'dns_responded': True}

    def updater(_, release):
        if failure == 'updater' and len(selected) == 1:
            unit.with_name('lovestdns-test-update.path').write_text('# partial updater setup')
            raise RuntimeError(failure)

    for name, fixture in {
        'paths': lambda _: paths, 'dependencies': lambda: None, 'install_lock': lambda _: nullcontext(),
        'is_active': lambda _: active, 'preflight_ports': lambda _: None, 'command': command,
        'switch': switch, 'healthy': probe, 'configure_online_updater': updater,
    }.items():
        monkeypatch.setattr(install, name, fixture)

    with pytest.raises(RuntimeError, match=failure):
        install.install(args, package_dir)
    assert not active and not paths['ctl'].exists()
    assert (paths['home'] / 'current/manifest.json').is_file()
    assert selected[0].is_dir() and (paths['home'] / 'receipts/test.1.json').is_file()
    if failure == 'updater':
        assert ['systemctl', 'disable', '--now', 'lovestdns-test-update.path'] in calls

    (source / 'rules').write_text('changed import source')
    install.install(args, package_dir)
    assert active and paths['ctl'].is_file()
    assert selected[0].name == 'test.1' and selected[1].name.startswith('test.1-retry-')
    assert (paths['storage'] / 'data/rules').read_text() == 'original imported rules'
    assert not (paths['home'] / 'rollback.json').exists()
    with pytest.raises(ValueError, match='version is already active'):
        install.install(args, package_dir)


def test_native_unit_keeps_host_resolver_and_uses_private_data_mount():
    paths = {key: PurePosixPath(str(value).replace('\\', '/')) if isinstance(value, Path) else value
             for key, value in install.paths('validation').items()}
    value = install.service_unit(paths, {**install.DEFAULTS, 'WEB_UI_TOKEN': 'test'})
    assert 'PrivateTmp=true' in value
    assert 'BindPaths=/var/lib/lovestdns-validation/data:/data' in value
    assert 'ProtectSystem=strict' in value
    assert '/etc/resolv.conf' not in value
    assert 'RootDirectory=' not in value and 'docker run' not in value
    with pytest.raises(ValueError):
        install.paths('../other')


@pytest.mark.skipif(sys.platform != 'linux' or not shutil.which('bash'), reason='GNU grep compatibility regression runs on Linux')
def test_native_marker_checksum_reads_nul_padded_upstream_header(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('patch_upstream', ROOT / 'packaging/integrated/patch_upstream.py')
    patcher = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'patch_upstream', patcher)
    spec.loader.exec_module(patcher)
    spec = importlib.util.spec_from_file_location('native_adapt', ROOT / 'packaging/native/adapt_upstream.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    digest = hashlib.sha512(b'upstream compressed marker data').hexdigest()
    header = tmp_path / 'marker.sha'
    header.write_bytes(digest.encode() + b'\0' * 896)
    script = adapter.adapt_mark_checksum('sp_sha_hash=$(grep -Eo "[0-9A-Za-z]{128}" "$sp_sha" | head -1)')
    script = 'sp_sha="$1"\n' + script + '\nprintf "%s" "$sp_sha_hash"\n'
    result = subprocess.run(['bash', '-c', script, 'checksum-test', str(header)], capture_output=True, text=True, check=True)
    assert result.stdout == digest and not result.stderr


@pytest.fixture
def native_component(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'integrated'))
    source = ROOT / 'packaging/native/runtime'
    for name, path, package_dirs in (
        ('native', source / '__init__.py', [str(source)]),
        ('native.environment', source / 'environment.py', None),
        ('native.component', source / 'component.py', None),
    ):
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=package_dirs)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return sys.modules['native.component']


@pytest.mark.parametrize('mode,role,expected', [('no', 'raw', '192.0.2.4'), ('yes', 'raw', '127.0.0.1'),
                                              ('yes', 'forward', '127.0.0.1')])
def test_imported_unbound_templates_get_native_paths_and_separate_controls(native_component, monkeypatch, tmp_path, mode, role, expected):
    component = native_component
    shared_prepare = component.shared.prepare_unbound
    monkeypatch.setattr(component.shared, 'prepare_unbound', lambda value: shared_prepare(value, directory=tmp_path))
    monkeypatch.setattr(component, 'Path', lambda name: tmp_path / Path(name).name)
    monkeypatch.setenv('CNAUTO', mode)
    monkeypatch.setenv('DNS_BIND_ADDRESS', '192.0.2.4')
    monkeypatch.setenv('FORWARD_BIND_ADDRESS', '127.0.0.1')
    path = tmp_path / f'unbound_{role}.conf'
    path.write_text('server:\n    interface: 0.0.0.0\n    root-hints: "/etc/unbound/named.cache"\n')
    component.prepare_unbound(role)
    value = path.read_text()
    assert 'directory: "/tmp"' in value and '/etc/unbound/' not in value
    assert f'interface: {expected}' in value
    assert f'control-interface: /tmp/uc_{role}.sock' in value


def test_native_environment_keeps_user_rule_variables_and_rejects_execution_overrides(native_component):
    validate = sys.modules['native.environment'].validate
    validate({'LOCAL_IP': '192.0.2.5', 'SUITE_LOCAL_IP': '192.0.2.6', 'CNAUTO': 'yes'})
    for key in ('PATH', 'PYTHONPATH', 'LD_PRELOAD', 'BASH_ENV', 'SUITE_DNS_PORT', 'WEB_UI_TOKEN', 'LOVESTDNS_HOME'):
        with pytest.raises(ValueError, match='Reserved'):
            validate({key: 'override'})


@pytest.mark.parametrize('address,expected', [('0.0.0.0', '0.0.0.0:15453'),
                                            ('::', '[::]:15453'),
                                            ('2001:db8::1', '[2001:db8::1]:15453')])
def test_native_mosdns_listeners_are_valid_yaml_for_ipv4_and_ipv6(native_component, monkeypatch, tmp_path, address, expected):
    import yaml
    path = tmp_path / 'mosdns.yaml'
    path.write_text('udp:\n  listen: :53\ntcp:\n  listen: :53\n')
    monkeypatch.setattr(native_component, 'Path', lambda name: path)
    monkeypatch.setenv('DNS_BIND_ADDRESS', address)
    monkeypatch.setenv('SUITE_DNS_PORT', '15453')
    native_component.render_mosdns()
    assert [entry['listen'] for entry in yaml.safe_load(path.read_text()).values()] == [expected, expected]


def test_native_unbound_runtime_includes_remain_valid_after_saved_file_changes(native_component, tmp_path):
    source, target = tmp_path / 'saved.conf', tmp_path / 'running.conf'
    source.write_text('server:\n    verbosity: 1\n')
    template = 'include-toplevel: "/data/unbound_custom.conf"\n'
    running = native_component.freeze_unbound_custom(template, source, target)
    source.write_text('server:\n    invalid-option: yes\n')
    assert '/data/unbound_custom.conf' not in running
    assert str(target) in running
    assert target.read_text() == 'server:\n    verbosity: 1\n'
    native_component.freeze_unbound_custom(template, source, target)
    assert 'invalid-option' in target.read_text()
