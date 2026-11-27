"""Reject unsafe base packages and retain executable modes across Windows builds."""
import io
import json
from pathlib import Path
import runpy
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
refresh = runpy.run_path(str(ROOT / 'scripts/refresh-native-package.py'))
bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))
LIBEXEC = {'libexec/' + binary for binaries in bundle['NAMES'].values() for binary in binaries}


def write_fixture(root, name, data=b'fixture'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def refresh_inputs(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    base = tmp_path / 'lovestdns-0.1.0'
    mosdns = write_fixture(base, 'libexec/mosdns', b'frozen mosdns')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    lock['frozen']['mosdns']['sha256'] = refresh['digest'](mosdns)
    protected = ('packaging/integrated/patch_upstream.py', 'packaging/native/adapt_upstream.py',
                 'packaging/native/requirements.lock.txt', 'packaging/components.lock.json', 'packaging/upstream.lock.json')
    for name in protected:
        write_fixture(source, name)
    refresh['write_json'](source / 'packaging/components.lock.json', lock)
    provenance = {'sha256': 'fixture source', 'files': {name: refresh['digest'](source / name) for name in protected}}
    for name in ('integrated/runtime/runtime.py', 'packaging/native/runtime/native.py', 'web/backend/app.py',
                 'packaging/native/install.py', 'docs/lovestdns-native-deployment.md', 'install.py'):
        write_fixture(source, name)
    write_fixture(source, 'integrated/backend/server.py', b'VERSION = "1.2.3"\n')
    for directory in ('app/runtime', 'app/backend', 'app/native', 'app/base', 'dist'):
        (base / directory).mkdir(parents=True)
    refresh['write_json'](base / 'source-provenance.json', provenance)
    refresh['write_json'](base / 'build-info.json', {})

    commit = 'a' * 40
    frontend = tmp_path / 'frontend'
    index = write_fixture(frontend, 'index.html')
    refresh['write_json'](frontend / 'build-manifest.json', {
        'schema_version': 1, 'source_commit': commit, 'source_snapshot': provenance, 'working_tree_modified': False,
        'dependency_mode': 'npm-ci', 'lock_sha256': refresh['digest'](ROOT / 'integrated/frontend/package-lock.json'),
        'artifacts': {'index.html': refresh['digest'](index)},
    })

    def load(path):
        relative = Path(path).relative_to(source).as_posix()
        if relative == 'scripts/export-api-contract.py':
            return {'source_summary': lambda: provenance}
        return runpy.run_path(str(ROOT / relative))

    globals_ = refresh['refresh'].__globals__
    monkeypatch.setitem(globals_, 'ROOT', source)
    monkeypatch.setitem(globals_, 'runpy', SimpleNamespace(run_path=load))
    monkeypatch.setitem(globals_, 'subprocess', SimpleNamespace(
        check_output=lambda command, **_: commit if command[1] == 'rev-parse' else ''))

    elf = b'\x7fELF\x02\x01' + b'\0' * 12 + b'\x3e\x00'
    filter_dir = tmp_path / 'filter'
    extension = runpy.run_path(str(ROOT / 'packaging/components/mosdns_filter.py'))
    filter_binary = write_fixture(filter_dir, 'mosdns-filter', elf)
    refresh['write_json'](filter_dir / 'manifest.json', {
        **extension['locked_source'](), 'target': 'linux/amd64', 'sha256': refresh['digest'](filter_binary),
    })
    return base, frontend, filter_dir, lock, elf


def test_formal_release_flag_requires_stable_version_and_keeps_performance_unclaimed():
    assert refresh['release_flags']('0.1.0', True) == {
        'candidate_only': False, 'release_approved': True, 'performance_acceptance': False}
    assert refresh['release_flags']('0.1.0-dev.example', False)['candidate_only']
    for version in ('0.1.0-dev.example', '01.1.0', '1.0', 'v1.0.0'):
        with pytest.raises(ValueError, match='semantic version'):
            refresh['release_flags'](version, True)


def test_extract_retains_linux_executable_mode(tmp_path):
    archive = tmp_path / 'base.tar'
    with tarfile.open(archive, 'w') as target:
        item = tarfile.TarInfo('native/core/redis-server')
        item.size = 5; item.mode = 0o755
        target.addfile(item, io.BytesIO(b'bytes'))
    package, modes = refresh['extract'](archive, tmp_path / 'extract')
    assert (package / 'core/redis-server').read_bytes() == b'bytes'
    assert modes['core/redis-server'] == 0o755


@pytest.mark.parametrize('fault', ['traversal', 'absolute', 'symlink', 'hardlink', 'duplicate', 'two-roots'])
def test_unsafe_base_rejected_before_extraction(tmp_path, fault):
    archive = tmp_path / 'base.tar'
    with tarfile.open(archive, 'w') as target:
        item = tarfile.TarInfo('native/file'); item.size = 1
        target.addfile(item, io.BytesIO(b'a'))
        bad = tarfile.TarInfo({'traversal': 'native/../outside', 'absolute': '/outside',
                              'two-roots': 'second/file'}.get(fault, 'native/file'))
        if fault in ('symlink', 'hardlink'):
            bad.name = 'native/link'; bad.type = tarfile.SYMTYPE if fault == 'symlink' else tarfile.LNKTYPE
            bad.linkname = '/outside'; target.addfile(bad)
        else:
            bad.size = 1; target.addfile(bad, io.BytesIO(b'b'))
    output = tmp_path / 'extract'
    with pytest.raises(ValueError): refresh['extract'](archive, output)
    assert not output.exists()


def test_bad_archive_hash_creates_no_output(tmp_path):
    archive = tmp_path / 'base.tar'; archive.write_bytes(b'unverified')
    output = tmp_path / 'candidate.tar.gz'
    with pytest.raises(ValueError, match='checksum'):
        refresh['refresh'](archive, '0' * 64, tmp_path / 'frontend', output)
    assert not output.exists()


@pytest.mark.parametrize('layout, release, excluded', [
    ('bundled', True, LIBEXEC),
    ('legacy', True, LIBEXEC),
    ('no-component-manifest', True, {'libexec/dnscrypt-proxy'}),
    ('legacy', False, set()),
])
def test_refresh_release_trims_only_verified_replacements(tmp_path, refresh_inputs, layout, release, excluded):
    base, frontend, filter_dir, lock, elf = refresh_inputs
    entries = {}
    launchers = {}
    for component, binaries in bundle['NAMES'].items():
        entry = {'binaries': {binary: 'bin/' + binary for binary in binaries}, 'runtime': {}}
        entries[component] = entry
        if component == 'dnscrypt' and layout != 'bundled':
            entry = {'binaries': {'dnscrypt-proxy': 'dnscrypt-proxy'}, 'runtime': {}}
        for binary, relative in entry['binaries'].items():
            data = elf if component == 'dnscrypt' else binary.encode()
            path = write_fixture(base, f'components/{component}/{relative}', data)
            launchers['core/' + binary] = bundle['wrapper'](component, binary, entry).encode('utf-8')
            write_fixture(base, 'core/' + binary, launchers['core/' + binary])
            write_fixture(base, 'libexec/' + binary)
            if component == 'dnscrypt':
                dnscrypt_sha = refresh['digest'](path)

    if layout != 'no-component-manifest':
        selected = list(entries) if layout == 'bundled' else ['redis', 'unbound']
        refresh['write_json'](base / 'components/manifest.json', {'components': entries, 'selected': selected})
    dnscrypt_lock = ROOT / 'packaging/dnscrypt-source.lock.json'
    dnscrypt = json.loads(dnscrypt_lock.read_text())
    refresh['write_json'](base / 'components/dnscrypt/source-manifest.json', {
        'candidate_only': True, 'diagnostic_only': False, 'release_approved': False, 'variant': 'combined',
        'version': dnscrypt['version'], 'architecture': 'linux/amd64', 'build_environment': {'CGO_ENABLED': '0'},
        'source': dnscrypt['source'], 'toolchain': dnscrypt['toolchain'], 'binary_sha256': dnscrypt_sha,
        'source_lock_sha256': refresh['digest'](dnscrypt_lock),
        'patches_sha256': {name: dnscrypt['patches'][name] for name in dnscrypt['variants']['combined']},
    })
    old_files = {path.relative_to(base).as_posix(): refresh['digest'](path) for path in base.rglob('*') if path.is_file()}
    components = {'selected': list(entries), 'lock_sha256': bundle['lock_digest'](lock),
                  'versions': {name: value['version'] for name, value in lock['components'].items()},
                  'source_candidate': {'binary_sha256': dnscrypt_sha}}
    refresh['write_json'](base / 'manifest.json', {'product': 'LovestDNS', 'deployment': 'native', 'architecture': 'amd64',
                                                'version': '0.1.0', 'components': components, 'files': old_files})
    archive = tmp_path / 'base.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(base, arcname=base.name)

    output = tmp_path / 'refresh.tar.gz'
    refresh['refresh'](archive, refresh['digest'](archive), frontend, output, release=release, mosdns_filter=filter_dir)

    package, _ = refresh['extract'](output, tmp_path / 'refreshed')
    inherited = json.loads((package / 'native-refresh-provenance.json').read_text())['inherited_files']
    files = json.loads((package / 'manifest.json').read_text())['files']
    assert {path.name for path in (package / 'libexec').iterdir()} == {
        Path(name).name for name in LIBEXEC - excluded} | {'mosdns'}
    assert excluded.isdisjoint(files) and excluded.isdisjoint(inherited)
    for name in (LIBEXEC - excluded) | {'libexec/mosdns'} | set(launchers):
        assert files[name] == inherited[name] == old_files[name]


def test_successive_refreshes_preserve_prior_provenance_without_legacy_name_collision(tmp_path):
    legacy = tmp_path / 'base-source-provenance.json'
    legacy.write_bytes(b'original component source')
    snapshots = []
    for generation in (1, 2):
        files = {name: f'generation {generation}: {name}'.encode() for name in
                 ('source-provenance.json', 'native-trial-provenance.json', 'native-refresh-provenance.json')}
        for name, data in files.items():
            (tmp_path / name).write_bytes(data)
        refresh['preserve_provenance'](tmp_path, str(generation) * 64)
        snapshots.append(files)
    assert legacy.read_bytes() == b'original component source'
    for generation, files in enumerate(snapshots, 1):
        for name, data in files.items():
            assert (tmp_path / 'base-provenance' / (str(generation) * 64) / name).read_bytes() == data
