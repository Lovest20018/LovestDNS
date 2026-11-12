"""Candidate packaging binds reviewed patches and preserves the other components."""
import hashlib
import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[2]
candidate = runpy.run_path(str(ROOT / 'packaging/components/dnscrypt_candidate.py'))
bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))


@pytest.fixture
def inputs(tmp_path):
    lock_path = ROOT / 'packaging/dnscrypt-source.lock.json'
    lock = json.loads(lock_path.read_text())
    source = tmp_path / 'candidate'
    source.mkdir()
    binary = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00' + b'candidate fixture'
    (source / 'dnscrypt-proxy').write_bytes(binary)
    manifest = {'candidate_only': True, 'diagnostic_only': False, 'release_approved': False,
                'variant': 'combined', 'architecture': 'linux/amd64', 'version': lock['version'],
                'source': lock['source'], 'toolchain': lock['toolchain'],
                'patches_sha256': {name: lock['patches'][name] for name in lock['variants']['combined']},
                'source_lock_sha256': candidate['digest'](lock_path),
                'binary_sha256': hashlib.sha256(binary).hexdigest(), 'build_environment': {'CGO_ENABLED': '0'}}
    (source / 'source-manifest.json').write_text(json.dumps(manifest))
    original = tmp_path / 'bundle'
    entries = {}
    component_lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    for name, binaries in bundle['NAMES'].items():
        (original / name / 'bin').mkdir(parents=True)
        for executable in binaries:
            (original / name / 'bin' / executable).write_bytes(b'original binary')
        entries[name] = {'version': component_lock['components'][name]['version'], 'runtime': {},
                         'binaries': {exe: 'bin/' + exe for exe in binaries}}
    original_manifest = {'format_version': 1, 'architecture': 'amd64', 'lock_sha256': bundle['lock_digest'](component_lock),
                         'components': entries, 'files': {p.relative_to(original).as_posix(): candidate['digest'](p)
                                                        for p in original.rglob('*') if p.is_file()}}
    (original / 'manifest.json').write_text(json.dumps(original_manifest))
    package = tmp_path / 'package'
    components = bundle['install'](original, package, component_lock)
    return source, package, components, original


def test_combined_package_declares_actual_payload_and_preserves_other_binaries(inputs):
    source, package, components, original = inputs
    previous = (package / 'components/manifest.json').read_bytes()
    result = candidate['install'](source, package, components)
    assert result['source_candidate']['variant'] == 'combined'
    assert result['candidate_only'] and not result['release_approved'] and not result['performance_acceptance']
    assert (package / 'components/bundle-input-manifest.json').read_bytes() == previous
    actual = json.loads((package / 'components/manifest.json').read_text())
    assert actual['format_version'] == 2
    assert actual['files']['dnscrypt/bin/dnscrypt-proxy'] == candidate['digest'](source / 'dnscrypt-proxy')
    assert all(candidate['digest'](package / 'components' / name) == sha for name, sha in actual['files'].items())
    for name in ('redis/bin/redis-server', 'unbound/bin/unbound'):
        assert (package / 'components' / name).read_bytes() == (original / name).read_bytes()
    assert 'components/dnscrypt/bin/dnscrypt-proxy' in (package / 'core/dnscrypt-proxy').read_text()
    assert (original / 'dnscrypt/bin/dnscrypt-proxy').read_bytes() == b'original binary'


@pytest.mark.parametrize('fault', ['variant', 'instrumented', 'cgo', 'architecture', 'lock', 'patch', 'binary', 'elf'])
def test_invalid_candidates_rejected_before_package_changes(inputs, fault):
    source, package, components, _ = inputs
    manifest_file = source / 'source-manifest.json'
    value = json.loads(manifest_file.read_text())
    if fault == 'variant': value['variant'] = 'cache'
    elif fault == 'instrumented': value['diagnostic_only'] = True
    elif fault == 'cgo': value['build_environment']['CGO_ENABLED'] = '1'
    elif fault == 'architecture': value['architecture'] = 'linux/arm64'
    elif fault == 'lock': value['source_lock_sha256'] = '0' * 64
    elif fault == 'patch': value['patches_sha256'] = {}
    else:
        (source / 'dnscrypt-proxy').write_bytes(b'wrong binary')
        if fault == 'elf': value['binary_sha256'] = candidate['digest'](source / 'dnscrypt-proxy')
    manifest_file.write_text(json.dumps(value))
    before = {p.relative_to(package): candidate['digest'](p) for p in package.rglob('*') if p.is_file()}
    with pytest.raises(ValueError): candidate['install'](source, package, components)
    assert {p.relative_to(package): candidate['digest'](p) for p in package.rglob('*') if p.is_file()} == before


def test_candidate_requires_explicit_dnscrypt_selection(inputs):
    source, package, components, _ = inputs
    components['selected'] = ['redis', 'unbound']
    with pytest.raises(ValueError, match='Select DNSCrypt'):
        candidate['install'](source, package, components)
