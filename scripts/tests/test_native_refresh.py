"""Reject unsafe base packages and retain executable modes across Windows builds."""
import io
from pathlib import Path
import runpy
import tarfile

import pytest

refresh = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/refresh-native-package.py'))


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
