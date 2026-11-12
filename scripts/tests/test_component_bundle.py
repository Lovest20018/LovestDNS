import hashlib
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))


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
    result = bundle['install'](source, target, lock, ['redis'])
    assert result['selected'] == ['redis']
    assert (target / 'core/mosdns').read_text() == 'frozen launcher'
    assert (target / 'lib/original-loader').read_text() == 'frozen runtime'
    assert not (target / 'components/unbound').exists()
    assert '$base/components/redis/bin/redis-server' in (target / 'core/redis-server').read_text()


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'bin/../escape', 'bin//file', 'bin/evil"name', 'bin/evil$name'])
def test_bundle_rejects_escaping_or_shell_interpolated_executable(tmp_path, path):
    source, lock = fixture(tmp_path)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['components']['unbound']['binaries']['unbound'] = path
    (source / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        bundle['verify'](source, lock)
