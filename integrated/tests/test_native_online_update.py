"""Exercise the worker with real verified archives and controlled host actions."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import time
from types import ModuleType, SimpleNamespace

import pytest
from runtime import software_update as protocol
from runtime import update_network

ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = '0.2.3-rc.2'
NEXT_VERSION = '0.2.3-rc.10'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def worker(monkeypatch):
    native = ModuleType('native')
    native.__path__ = [str(ROOT / 'packaging/native/runtime')]
    download = load('native.release_download', ROOT / 'install.py')
    native.release_download = download
    monkeypatch.setitem(sys.modules, 'native', native)
    monkeypatch.setitem(sys.modules, 'native.release_download', download)
    return load('native.update_worker', ROOT / 'packaging/native/runtime/update_worker.py')


@pytest.fixture
def instance(tmp_path):
    home, storage = tmp_path / 'home', tmp_path / 'storage'
    (home / 'current').mkdir(parents=True)
    (home / 'current/manifest.json').write_text(json.dumps({'version': CURRENT_VERSION}))
    for name in ('data', 'state', 'updates'):
        (storage / name).mkdir(parents=True)
    (storage / 'data/custom_env.ini').write_text('CNAUTO="yes"\n')
    root = storage / 'updates'
    protocol.atomic_json(root / 'protocol.json', {'schema_version': 1})
    return {'name': 'lovestdns-fixture', 'home': home, 'storage': storage}


def bundle(tmp_path, bad_inner=False):
    path = tmp_path / 'release.tar.gz'
    content = b'# Verified installer fixture; host actions are controlled by the test.\n'
    manifest = {'product': 'LovestDNS', 'deployment': 'native', 'architecture': 'amd64',
        'version': NEXT_VERSION, 'installation_receipt_v1': True,
        'files': {'install.py': ('0' * 64 if bad_inner else hashlib.sha256(content).hexdigest())}}
    with tarfile.open(path, 'w:gz') as archive:
        for name, payload in [('install.py', content), ('manifest.json', json.dumps(manifest).encode())]:
            entry = tarfile.TarInfo(f'lovestdns-{NEXT_VERSION}/{name}')
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(instance, archive, digest):
    root = instance['storage'] / 'updates'
    release = {'version': NEXT_VERSION, 'archive_sha256': digest, 'archive_bytes': archive.stat().st_size,
        'notes': '', 'url': f'https://github.com/Lovest20018/LovestDNS/releases/tag/v{NEXT_VERSION}',
        'prerelease': True, 'published_at': '2026-01-01T00:00:00Z'}
    protocol.atomic_json(root / 'status.json', {'phase': 'ready', 'release': release, 'checked_at': time.time()})
    protocol.queue(root, {'action': 'apply', 'version': NEXT_VERSION, 'archive_sha256': digest}, CURRENT_VERSION)
    return root, release


def fixture_download(monkeypatch, worker, archive, digest, transports=None):
    def download(version, destination, *, opener):
        assert version == NEXT_VERSION
        assert opener is not None
        if transports is not None:
            assert transports == [opener]
        target = Path(destination) / protocol.ASSET
        shutil.copyfile(archive, target)
        return target, digest
    monkeypatch.setattr(worker.release_download, 'download', download)


def test_worker_installs_verified_archive_records_digest_and_removes_downloads(worker, instance, tmp_path, monkeypatch):
    archive, digest = bundle(tmp_path)
    profile = {'mode': 'socks5', 'host': '192.0.2.10', 'port': 1080,
               'username': 'fixture-user', 'password': 'synthetic-secret', 'channel': 'preview'}
    update_network.save(instance['storage'] / 'updates', profile, CURRENT_VERSION)
    root, release = prepare(instance, archive, digest)
    transports = []
    def metadata(_, *, opener, channel):
        assert channel == 'preview'
        handlers = [h for h in opener.handlers if isinstance(h, update_network.TunnelHandler)]
        assert len(handlers) == 1 and handlers[0].settings == profile
        transports.append(opener)
        return release
    monkeypatch.setattr(protocol, 'checked_release', metadata)
    fixture_download(monkeypatch, worker, archive, digest, transports)
    calls = []
    def install(args, package):
        assert json.loads((package / 'manifest.json').read_text())['version'] == NEXT_VERSION
        assert args.instance == 'fixture' and args.sha256 == digest
        calls.append(package)
        (instance['home'] / 'current/manifest.json').write_text(json.dumps({'version': NEXT_VERSION}))
        (instance['home'] / 'receipts').mkdir()
        protocol.atomic_json(instance['home'] / 'receipts/current.json', {'archive_sha256': digest})
    monkeypatch.setattr(worker, 'runpy', SimpleNamespace(run_path=lambda _: {'install': install}))
    monkeypatch.setattr(worker, 'healthy_current', lambda _: True)
    worker.run(instance, 'fixture')
    value = protocol.read_json(root / 'status.json')
    assert len(calls) == 1 and value['phase'] == 'succeeded' and value['healthy']
    assert value['installed_version'] == NEXT_VERSION and value['archive_sha256'] == digest
    assert not (root / 'request.json').exists() and not list(root.glob('work-*'))
    assert (instance['storage'] / 'data/custom_env.ini').read_text() == 'CNAUTO="yes"\n'
    assert update_network.load(root) == profile
    assert profile['password'].encode() not in (root / 'status.json').read_bytes()


@pytest.mark.parametrize('fault', ['metadata', 'outer', 'inner', 'install'])
def test_failed_update_leaves_previous_version_and_reports_health(worker, instance, tmp_path, monkeypatch, fault):
    archive, digest = bundle(tmp_path, bad_inner=fault == 'inner')
    if fault == 'outer':
        archive.write_bytes(archive.read_bytes() + b'corrupted')
    root, release = prepare(instance, archive, digest)
    monkeypatch.setattr(protocol, 'checked_release', lambda _, **kwargs: {**release, 'archive_sha256': 'b' * 64} if fault == 'metadata' else release)
    fixture_download(monkeypatch, worker, archive, digest)
    calls = []
    def install(*_):
        calls.append(True)
        raise RuntimeError('controlled installer failure')
    monkeypatch.setattr(worker, 'runpy', SimpleNamespace(run_path=lambda _: {'install': install}))
    monkeypatch.setattr(worker, 'healthy_current', lambda _: True)
    worker.run(instance, 'fixture')
    value = protocol.read_json(root / 'status.json')
    assert value['phase'] == 'failed' and value['installed_version'] == CURRENT_VERSION
    assert value['previous_version_healthy'] and value['healthy']
    assert len(calls) == (1 if fault == 'install' else 0)
    assert not list(root.glob('work-*'))


def test_worker_check_does_not_install_or_stop_dns(worker, instance, monkeypatch):
    root = instance['storage'] / 'updates'
    protocol.queue(root, {'action': 'check'}, CURRENT_VERSION)
    monkeypatch.setattr(protocol, 'checked_release', lambda **kwargs: {'version': '0.2.2'})
    monkeypatch.setattr(worker, 'apply', lambda *_: pytest.fail('Checking attempted installation'))
    worker.run(instance, 'fixture')
    value = protocol.read_json(root / 'status.json')
    assert value['phase'] == 'ready' and value['release']['version'] == '0.2.2' and value['checked_at'] > 0
    assert value['message'] == '当前版本高于所选通道的最新版本'


def test_worker_rejects_downgrade_queued_by_an_older_web_service(worker, instance, monkeypatch):
    root = instance['storage'] / 'updates'
    job = {'job_id': 'a' * 32, 'action': 'apply', 'created_at': time.time(), 'previous_version': CURRENT_VERSION,
           'version': '0.2.2', 'archive_sha256': 'b' * 64}
    protocol.atomic_json(root / 'request.json', job)
    protocol.atomic_json(root / 'status.json', {**job, 'phase': 'queued'})
    monkeypatch.setattr(protocol, 'checked_release', lambda *args, **kwargs: pytest.fail('Downgrade reached the network'))
    monkeypatch.setattr(worker, 'healthy_current', lambda _: True)

    worker.run(instance, 'fixture')

    value = protocol.read_json(root / 'status.json')
    assert value['phase'] == 'failed' and value['installed_version'] == CURRENT_VERSION
    assert '当前版本高于所选通道的最新版本' in value['message']
    assert value['previous_version_healthy']
    assert not (root / 'request.json').exists()


@pytest.mark.parametrize('bad', ['json', 'mismatched_job', 'extra_command'])
def test_corrupt_request_is_quarantined_without_path_trigger_loop(worker, instance, monkeypatch, bad):
    root = instance['storage'] / 'updates'
    protocol.queue(root, {'action': 'check'}, CURRENT_VERSION)
    job = protocol.read_json(root / 'request.json')
    if bad == 'json': (root / 'request.json').write_text('{invalid')
    elif bad == 'mismatched_job': protocol.atomic_json(root / 'request.json', {**job, 'job_id': 'a' * 32})
    else: protocol.atomic_json(root / 'request.json', {**job, 'command': 'not allowed'})
    monkeypatch.setattr(protocol, 'checked_release', lambda **kwargs: pytest.fail('Invalid request reached the network'))
    worker.run(instance, 'fixture')
    assert not (root / 'request.json').exists() and (root / 'rejected-request.json').exists()
    assert protocol.read_json(root / 'status.json')['phase'] == 'failed'


@pytest.mark.parametrize('healthy', [True, False])
def test_recovery_confirms_actual_installation_instead_of_replaying(worker, instance, monkeypatch, healthy):
    root = instance['storage'] / 'updates'
    (instance['home'] / 'current/manifest.json').write_text(json.dumps({'version': NEXT_VERSION}))
    (instance['home'] / 'receipts').mkdir()
    protocol.atomic_json(instance['home'] / 'receipts/current.json', {'archive_sha256': 'a' * 64})
    protocol.atomic_json(root / 'status.json', {'phase': 'installing', 'action': 'apply',
        'version': NEXT_VERSION, 'previous_version': CURRENT_VERSION, 'archive_sha256': 'a' * 64})
    def probe(_):
        if not healthy: raise RuntimeError('not healthy')
        return True
    monkeypatch.setattr(worker, 'healthy_current', probe)
    monkeypatch.setattr(worker, 'apply', lambda *_: pytest.fail('Interrupted job was replayed'))
    worker.recover(instance)
    value = protocol.read_json(root / 'status.json')
    assert value['phase'] == ('succeeded' if healthy else 'failed')
    assert value['healthy'] is healthy


def test_updater_units_survive_web_restart_and_are_disabled_for_old_release(tmp_path, monkeypatch):
    installer = load('online_native_install', ROOT / 'packaging/native/install.py')
    storage, home = tmp_path / 'storage', tmp_path / 'home'
    storage.mkdir()
    release = home / 'releases/test.next'
    (release / 'app/native').mkdir(parents=True)
    (release / 'app/native/update_worker.py').write_text('# fixture')
    unit = tmp_path / 'systemd/lovestdns-fixture.service'
    unit.parent.mkdir()
    p = {'name': 'lovestdns-fixture', 'storage': storage, 'home': home,
         'unit': unit, 'config': tmp_path / 'config/service.env'}
    calls = []
    monkeypatch.setattr(installer, 'command', lambda args, **kwargs: calls.append(args))
    installer.configure_online_updater(p, release)
    worker_unit = unit.with_name('lovestdns-fixture-update.service').read_text()
    assert 'KillMode=mixed' in worker_unit and 'PartOf=' not in worker_unit
    assert 'Type=oneshot' in worker_unit and '--instance fixture' in worker_unit
    assert str(release) in worker_unit and 'ProtectSystem=strict' in worker_unit
    assert ['systemctl', 'enable', '--now', 'lovestdns-fixture-update.path'] in calls
    root = storage / 'updates'
    protocol.atomic_json(root / 'status.json', {'phase': 'succeeded'})
    old = home / 'releases/test.old'
    old.mkdir()
    installer.configure_online_updater(p, old)
    assert not (root / 'protocol.json').exists()
    assert protocol.read_json(root / 'status.json')['phase'] == 'succeeded'
    assert ['systemctl', 'disable', '--now', 'lovestdns-fixture-update.path'] in calls
