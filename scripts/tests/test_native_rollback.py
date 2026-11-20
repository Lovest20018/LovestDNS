"""A failed manual restore must leave the current instance usable."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('failure', ['copy', 'second_switch', 'health'])
def test_manual_restore_recovers_current_instance(tmp_path, monkeypatch, failure):
    spec = importlib.util.spec_from_file_location('native_installer', Path(__file__).resolve().parents[2] / 'packaging/native/install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    p = {key: tmp_path / key for key in ('storage', 'home', 'unit', 'config')}
    p['name'] = 'synthetic'
    current = p['home'] / 'current'
    releases = p['home'] / 'releases'
    prior, target = releases / 'new', releases / 'old'
    for release in (prior, target):
        release.mkdir(parents=True)
    try:
        current.symlink_to(prior, target_is_directory=True)
    except OSError:
        pytest.skip('release symlinks unavailable')
    backup = p['storage'] / 'backups' / 'good'
    backup.mkdir(parents=True)
    (backup / 'release.json').write_text(json.dumps({'release': str(target)}))
    for name in ('data', 'state'):
        for directory, value in ((p['storage'], 'current'), (backup, 'backup')):
            (directory / name).mkdir()
            (directory / name / 'sample').write_text(value)
    for key, name in (('unit', 'service.unit'), ('config', 'service.env')):
        p[key].write_text('current')
        (backup / name).write_text('backup')

    commands = []
    monkeypatch.setattr(installer, 'command', lambda cmd, **kw: commands.append(cmd))
    monkeypatch.setattr(installer, 'is_active', lambda _: True)
    monkeypatch.setattr(installer, 'configure_online_updater', lambda *args: None)
    def healthy(_):
        if current.resolve() == target:
            raise RuntimeError('injected health failure')
        assert (p['storage'] / 'data/sample').read_text() == 'current'
    monkeypatch.setattr(installer, 'healthy', healthy)
    if failure == 'copy':
        def copy(*args, **kwargs):
            raise OSError('injected copy failure')
        monkeypatch.setattr(installer.shutil, 'copytree', copy)
    elif failure == 'second_switch':
        rename = Path.rename
        def fail_second(path, destination):
            if path.name == 'state' and path.parent.name.startswith('.rollback-'):
                raise OSError('injected switch failure')
            return rename(path, destination)
        monkeypatch.setattr(Path, 'rename', fail_second)

    with pytest.raises((OSError, RuntimeError), match='injected'):
        installer.restore(p, backup, manual=True)
    assert current.resolve() == prior
    assert all(p[key].read_text() == 'current' for key in ('unit', 'config'))
    assert all((p['storage'] / name / 'sample').read_text() == 'current' for name in ('data', 'state'))
    if failure == 'copy':
        assert not commands
    else:
        assert commands[-1] == ['systemctl', 'start', 'synthetic.service']
