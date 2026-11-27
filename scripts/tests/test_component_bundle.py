import hashlib
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))
UPSTREAM_BINARIES = {'redis-server', 'redis-cli', 'unbound', 'unbound-checkconf', 'dnscrypt-proxy', 'mosdns'}


def fixture(tmp_path):
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    source = tmp_path / 'bundle'
    entries = {}
    for name, names in bundle['NAMES'].items():
        (source / name / 'bin').mkdir(parents=True)
        for binary in names:
            (source / name / 'bin' / binary).write_text('immutable executable')
        entries[name] = {'version': lock['components'][name]['version'], 'runtime': {},
                         'binaries': {binary: 'bin/' + binary for binary in names}}
    manifest = {'format_version': 1, 'architecture': 'amd64', 'lock_sha256': bundle['lock_digest'](lock),
                'components': entries, 'files': {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                                for p in source.rglob('*') if p.is_file()}}
    (source / 'manifest.json').write_text(json.dumps(manifest))
    return source, lock


def add_upstream_binaries(target):
    upstream = target / 'libexec'
    upstream.mkdir(parents=True)
    for binary in UPSTREAM_BINARIES:
        (upstream / binary).write_text('upstream ' + binary)
    return upstream


def test_bundle_rejects_tamper_unlisted_files_and_other_lock(tmp_path):
    source, lock = fixture(tmp_path)
    assert bundle['verify'](source, lock)['architecture'] == 'amd64'
    executable = source / 'unbound/bin/unbound'
    executable.write_text('tampered executable')
    with pytest.raises(ValueError, match='checksum'):
        bundle['verify'](source, lock)
    executable.write_text('immutable executable')
    extra = source / 'unexpected'
    extra.write_text('unlisted')
    with pytest.raises(ValueError, match='Unexpected'):
        bundle['verify'](source, lock)
    extra.unlink()
    lock['components']['redis']['version'] = 'unreviewed'
    with pytest.raises(ValueError, match='locked'):
        bundle['verify'](source, lock)


def test_single_component_install_preserves_frozen_launcher_and_runtime(tmp_path):
    source, lock = fixture(tmp_path)
    target = tmp_path / 'package'
    (target / 'core').mkdir(parents=True)
    (target / 'core/mosdns').write_text('frozen launcher')
    (target / 'lib').mkdir()
    (target / 'lib/original-loader').write_text('frozen runtime')
    upstream = add_upstream_binaries(target)
    result = bundle['install'](source, target, lock, ['redis'])
    assert result['selected'] == ['redis']
    assert (target / 'core/mosdns').read_text() == 'frozen launcher'
    assert (target / 'lib/original-loader').read_text() == 'frozen runtime'
    assert {path.name: path.read_text() for path in upstream.iterdir()} == {
        name: 'upstream ' + name for name in {'unbound', 'unbound-checkconf', 'dnscrypt-proxy', 'mosdns'}}
    assert not (target / 'components/unbound').exists()
    assert '$base/components/redis/bin/redis-server' in (target / 'core/redis-server').read_text()


def test_all_selected_native_components_omit_only_superseded_upstream_binaries(tmp_path):
    source, lock = fixture(tmp_path)
    target = tmp_path / 'package'
    upstream = add_upstream_binaries(target)
    result = bundle['install'](source, target, lock, ['redis', 'unbound', 'dnscrypt'])
    assert set(result['selected']) == {'redis', 'unbound', 'dnscrypt'}
    assert {path.name for path in upstream.iterdir()} == {'mosdns'}
    assert (upstream / 'mosdns').read_text() == 'upstream mosdns'


@pytest.mark.parametrize('component', ['redis', 'unbound', 'dnscrypt'])
@pytest.mark.parametrize('fault', ['launcher', 'line-endings', 'executable'])
def test_native_component_omission_validates_every_binary_before_removing_any(tmp_path, component, fault):
    source, lock = fixture(tmp_path)
    target = tmp_path / 'package'
    bundle['install'](source, target, lock, [component])
    upstream = add_upstream_binaries(target)
    entry = bundle['verify'](source, lock)['components'][component]
    binary = sorted(bundle['NAMES'][component])[-1]
    launcher = target / 'core' / binary
    if fault == 'launcher':
        launcher.write_bytes(b'upstream launcher\n')
    elif fault == 'line-endings':
        launcher.write_bytes(launcher.read_bytes().replace(b'\n', b'\r\n'))
    else:
        (target / 'components' / component / entry['binaries'][binary]).unlink()
    with pytest.raises(ValueError, match='launcher'):
        bundle['omit_superseded_native_component'](target, component, entry)
    assert {path.name: path.read_text() for path in upstream.iterdir()} == {
        name: 'upstream ' + name for name in UPSTREAM_BINARIES}


def test_selected_native_dnscrypt_omits_only_the_shadowed_upstream_binary(tmp_path):
    source, lock = fixture(tmp_path)
    target = tmp_path / 'package'
    (target / 'libexec').mkdir(parents=True)
    upstream = target / 'libexec/dnscrypt-proxy'
    upstream.write_text('upstream DNSCrypt')
    result = bundle['install'](source, target, lock, ['dnscrypt'])
    assert result['selected'] == ['dnscrypt']
    assert not upstream.exists()
    assert 'components/dnscrypt/bin/dnscrypt-proxy' in (target / 'core/dnscrypt-proxy').read_text()
    assert (target / 'components/dnscrypt/bin/dnscrypt-proxy').read_text() == 'immutable executable'


def test_legacy_dnscrypt_omission_validates_the_launcher(tmp_path):
    (tmp_path / 'core').mkdir()
    (tmp_path / 'components/dnscrypt').mkdir(parents=True)
    (tmp_path / 'libexec').mkdir()
    launcher = tmp_path / 'core/dnscrypt-proxy'
    launcher.write_bytes(b'upstream launcher\n')
    (tmp_path / 'components/dnscrypt/dnscrypt-proxy').write_text('reviewed DNSCrypt')
    upstream = tmp_path / 'libexec/dnscrypt-proxy'
    upstream.write_text('upstream DNSCrypt')
    entry = {'binaries': {'dnscrypt-proxy': 'dnscrypt-proxy'}, 'runtime': {}}
    with pytest.raises(ValueError, match='launcher'):
        bundle['omit_superseded_native_dnscrypt'](tmp_path, entry)
    assert upstream.read_text() == 'upstream DNSCrypt'
    launcher.write_bytes(bundle['wrapper']('dnscrypt', 'dnscrypt-proxy', entry).encode('utf-8'))
    bundle['omit_superseded_native_dnscrypt'](tmp_path, entry)
    assert not upstream.exists()


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'bin/../escape', 'bin//file', 'bin/evil"name', 'bin/evil$name'])
def test_bundle_rejects_escaping_or_shell_interpolated_executable(tmp_path, path):
    source, lock = fixture(tmp_path)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['components']['unbound']['binaries']['unbound'] = path
    (source / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        bundle['verify'](source, lock)
