import importlib.util
import json
from pathlib import Path
import sys

import pytest

from runtime.maintenance import ActionFailed, CoreActions
from runtime.manager import Manager
from runtime.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def restart_module(monkeypatch):
    source = ROOT / 'packaging/native/runtime'
    for name, file, locations in [('native', '__init__.py', [str(source)]),
                                   ('native.environment', 'environment.py', None),
                                   ('native.restart', 'restart.py', None)]:
        spec = importlib.util.spec_from_file_location(name, source / file, submodule_search_locations=locations)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return sys.modules['native.restart']


class Core(CoreActions):
    def __init__(self, root):
        super().__init__(root / 'state', root / 'run', root / 'data', root / 'tmp')
        for p in (self.state, self.run, self.data, self.temporary):
            p.mkdir()
        (self.run / 'ready').touch()
        (self.run / 'core-env.json').write_text('{"CNAUTO":"yes"}')
        (self.run / 'mosdns-env.json').write_text('{"OLD_VALUE":"preserved"}')
        (self.data / 'custom_env.ini').write_text('CNAUTO="no"\n')
        (self.temporary / 'mosdns.yaml').write_text('old runtime')
        self.processes = {name: {'statename': 'RUNNING', 'pid': i + 1, 'start': 100, 'exitstatus': 0}
                          for i, name in enumerate(['redis', 'mosdns', 'unbound-raw', 'watcher', 'web', 'manager'])}
        self.processes['bootstrap'] = {'statename': 'EXITED', 'exitstatus': 0}
        self.events = []

    def states(self):
        return {name: dict(value) for name, value in self.processes.items()}

    def save_snapshot_unlocked(self):
        self.events.append(('snapshot', 'redis'))

    def wait_running(self, name, timeout=8):
        assert self.processes[name]['statename'] == 'RUNNING'

    def probe(self):
        return {'responded': True, 'rcode': 3}


@pytest.mark.parametrize('mode', ['yes', 'no'])
@pytest.mark.parametrize('fail', [False, True])
def test_restart_switches_modes_and_recovers_previous_runtime_without_stopping_web(tmp_path, monkeypatch, restart_module, mode, fail):
    core = Core(tmp_path)
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '1')
    old = core.states()

    def control(action, name):
        core.events.append((action, name))
        core.processes.setdefault(name, {})['statename'] = 'RUNNING' if action == 'start' else 'STOPPED'
        if name == 'bootstrap' and action == 'start':
            (core.temporary / 'mosdns.yaml').write_text('candidate runtime')
            (core.run / 'core-env.json').write_text(json.dumps({'CNAUTO': mode}))
            core.processes[name] = {'statename': 'EXITED', 'exitstatus': 47 if fail else 0}
            if not fail:
                for i, required in enumerate(['redis', 'unbound-raw', 'watcher'] + (['mosdns', 'unbound-forward', 'dnscrypt'] if mode == 'yes' else [])):
                    core.processes[required] = {'statename': 'RUNNING', 'pid': i + 20, 'start': 200}
                (core.run / 'ready').touch()

    monkeypatch.setattr(restart_module, 'control', control)
    if fail:
        with pytest.raises(ActionFailed) as error:
            core.execute({'action': 'restart_dns_core'})
        assert error.value.details['rollback']['runtime_restored']
        assert error.value.details['rollback']['service_running']
        assert (core.temporary / 'mosdns.yaml').read_text() == 'old runtime'
        assert json.loads((core.run / 'core-env.json').read_text()) == {'CNAUTO': 'yes'}
        assert json.loads((core.run / 'mosdns-env.json').read_text()) == {'OLD_VALUE': 'preserved'}
    else:
        result = core.execute({'action': 'restart_dns_core'})
        assert result['state'] == 'restarted_unverified' and result['verified'] is False
        assert core.environment()['CNAUTO'] == mode
    assert core.events[0] == ('snapshot', 'redis')
    assert not any(name in {'web', 'manager', 'observer'} for _, name in core.events)
    assert core.states()['web'] == old['web'] and core.states()['manager'] == old['manager']
    assert (core.run / 'ready').exists()
    assert (core.data / 'custom_env.ini').read_text() == 'CNAUTO="no"\n'


@pytest.mark.parametrize('failure', ['snapshot', 'reserved', 'container', 'initializing'])
def test_restart_preflight_failure_never_stops_components(tmp_path, monkeypatch, restart_module, failure):
    core = Core(tmp_path)
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '0' if failure == 'container' else '1')
    if failure == 'snapshot':
        monkeypatch.setattr(core, 'save_snapshot_unlocked', lambda: (_ for _ in ()).throw(ValueError('snapshot failed')))
    elif failure == 'reserved':
        (core.data / 'custom_env.ini').write_text('PATH="untrusted"\n')
    elif failure == 'initializing':
        core.processes['bootstrap']['statename'] = 'RUNNING'
    monkeypatch.setattr(restart_module, 'control', lambda *_: pytest.fail('Preflight failure stopped a process'))
    with pytest.raises(ValueError):
        core.execute({'action': 'restart_dns_core'})
    assert not core.events


def test_restart_task_is_native_only_and_deduplicated(tmp_path, monkeypatch):
    core = Core(tmp_path)
    manager = Manager(core, TaskStore(core.state))
    monkeypatch.setattr(manager, 'healthy', lambda: True)  # No background execution in this submission test.
    request = {'operation': 'submit', 'task': {'action': 'restart_dns_core', 'request_id': 'c' * 32, 'expected_sha256': None}}
    monkeypatch.delenv('LOVESTDNS_SYSTEMD', raising=False)
    with pytest.raises(ValueError):
        manager.dispatch(request)
    assert not manager.store.list()
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '1')
    assert manager.dispatch(request)['task']['id'] == manager.dispatch(request)['task']['id']
    assert len(manager.store.list()) == 1
    (core.run / 'ready').unlink()
    assert manager.dispatch(request)['task']['id'] == manager.store.list()[0]['id']


def test_streaming_backup_retains_manifest_and_rejects_corruption_before_restore(tmp_path, monkeypatch, restart_module):
    from native.backup import create
    core = Core(tmp_path)
    path = core.temporary / 'large.conf'
    path.write_bytes(b'x' * (1024 * 1024))
    original = Path.read_bytes
    def bounded_only(value):
        assert value != path, 'Runtime configuration must be streamed'
        return original(value)
    monkeypatch.setattr(Path, 'read_bytes', bounded_only)
    backup = create(core)
    manifest = json.loads((core.state / 'last-runtime-before-restart.json').read_text())
    assert manifest['schema_version'] == 1
    assert (core.state / 'last-runtime-before-restart.json').stat().st_size < 4096
    assert all(len(item['sha256']) == 64 for item in manifest['files'])
    item = next(item for item in manifest['files'] if item['name'] == 'large.conf')
    (backup.directory / item['blob']).write_bytes(b'corrupt')
    (core.temporary / 'mosdns.yaml').write_text('still running')
    with pytest.raises(ValueError, match='校验失败'):
        backup.restore()
    assert (core.temporary / 'mosdns.yaml').read_text() == 'still running'


def test_backup_rejects_path_escape(tmp_path, restart_module):
    from native.backup import RuntimeBackup
    core = Core(tmp_path)
    manifest = {'files': [{'group': 'temporary', 'name': '../outside', 'blob': '0000'}]}
    with pytest.raises(ValueError, match='路径无效'):
        RuntimeBackup(core, core.state, manifest)
