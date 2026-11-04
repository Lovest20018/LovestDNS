import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import sys

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
