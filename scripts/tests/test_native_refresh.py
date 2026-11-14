"""Reject unsafe base packages and retain executable modes across Windows builds."""
import io
from pathlib import Path
import runpy
import tarfile

import pytest

refresh = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/refresh-native-package.py'))


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
