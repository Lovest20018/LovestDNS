"""Production download entry must reject damaged/untrusted payloads before execution."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_install', ROOT / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def bundle(tmp_path, extra=None):
    path = tmp_path / 'native.tar.gz'
    content = b'raise SystemExit("test installer must not execute")\n'
    manifest = {'product': 'LovestDNS', 'deployment': 'native', 'architecture': 'amd64', 'version': 'test.1',
                'files': {'install.py': hashlib.sha256(content).hexdigest()}}
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in [('lovestdns-test.1/install.py', content),
                           ('lovestdns-test.1/manifest.json', json.dumps(manifest).encode())]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if extra:
            archive.addfile(extra)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_download_only_checks_real_archive_without_executing_installer(tmp_path):
    package, digest = bundle(tmp_path)
    destination = tmp_path / 'verified'
    result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--package', str(package),
                             '--sha256', digest, '--download-only', str(destination)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (destination / installer.ASSET).read_bytes() == package.read_bytes()
    assert (destination / (installer.ASSET + '.sha256')).read_text() == digest + '  ' + installer.ASSET + '\n'


def test_corrupt_package_never_reaches_extraction(tmp_path):
    package, digest = bundle(tmp_path)
    package.write_bytes(package.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='SHA-256 differs'):
        installer.unpack(package, digest, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


@pytest.mark.parametrize('kind', ['escape', 'link', 'duplicate', 'device', 'drive', 'normalized_duplicate', 'empty'])
def test_untrusted_archive_entries_are_rejected_before_extracting(tmp_path, kind):
    member = tarfile.TarInfo({'escape': '../escaped', 'link': 'lovestdns-test.1/link',
                             'duplicate': 'lovestdns-test.1/install.py', 'device': 'lovestdns-test.1/device',
                             'drive': 'C:/escaped', 'normalized_duplicate': 'lovestdns-test.1/./install.py', 'empty': '.'}[kind])
    if kind == 'link':
        member.type, member.linkname = tarfile.SYMTYPE, '/etc/passwd'
    if kind == 'device':
        member.type = tarfile.CHRTYPE
    package, digest = bundle(tmp_path, member)
    with pytest.raises(ValueError, match='Unsafe native package entry'):
        installer.unpack(package, digest, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_release_metadata_cannot_redirect_to_another_repository(monkeypatch, tmp_path):
    def fake_fetch(url, target, limit):
        Path(target).write_text(json.dumps({'tag_name': 'vtest.1', 'assets': [
            {'name': installer.ASSET, 'browser_download_url': 'https://github.com/other/repo/releases/download/vtest.1/' + installer.ASSET}]}))
    monkeypatch.setattr(installer, 'fetch', fake_fetch)
    with pytest.raises(ValueError, match='unexpected release asset'):
        installer.download('test.1', tmp_path)
