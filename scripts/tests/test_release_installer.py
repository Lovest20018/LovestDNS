"""Production download entry must reject damaged/untrusted payloads before execution."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_install', ROOT / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def bundle(tmp_path, extra=None, receipt_capable=False):
    path = tmp_path / 'native.tar.gz'
    content = b'raise SystemExit("test installer must not execute")\n'
    manifest = {'product': 'LovestDNS', 'deployment': 'native', 'architecture': 'amd64', 'version': 'test.1',
                'files': {'install.py': hashlib.sha256(content).hexdigest()}}
    if receipt_capable:
        manifest['installation_receipt_v1'] = True
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


@pytest.mark.parametrize('receipt_capable', [False, True])
def test_verified_archive_reaches_only_supported_installer_receipt_path(tmp_path, monkeypatch, receipt_capable):
    package, digest = bundle(tmp_path, receipt_capable=receipt_capable)
    monkeypatch.setattr(installer, 'os', SimpleNamespace(geteuid=lambda: 0))
    monkeypatch.setattr(installer.sys, 'argv', ['install.py', '--package', str(package), '--sha256', digest,
                                             '--instance', 'validation'])
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    installer.main()
    assert len(calls) == 1
    assert calls[0][2:5] == ['--instance', 'validation', 'upgrade' if receipt_capable else 'install']
    if receipt_capable:
        assert calls[0][5:] == [str(package.resolve()), '--sha256', digest]
    else:
        assert len(calls[0]) == 5


def test_corrupt_package_never_reaches_extraction(tmp_path):
    package, digest = bundle(tmp_path)
    package.write_bytes(package.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='SHA-256 differs'):
        installer.unpack(package, digest, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_debian_12_original_python_without_tar_extraction_filters(monkeypatch, tmp_path):
    package, digest = bundle(tmp_path)
    original = tarfile.TarFile.extractall
    monkeypatch.delattr(tarfile, 'data_filter', raising=False)
    def legacy_extractall(self, path='.', members=None, *, numeric_owner=False):
        return original(self, path, members=members, numeric_owner=numeric_owner, filter=lambda member, path: member)
    monkeypatch.setattr(tarfile.TarFile, 'extractall', legacy_extractall)
    root = installer.unpack(package, digest, tmp_path / 'output')
    assert (root / 'install.py').is_file()


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


def test_download_urls_are_fixed_to_the_selected_repository_and_exact_tag(monkeypatch, tmp_path):
    calls = []
    content = b'native-package-bytes'
    digest = hashlib.sha256(content).hexdigest()
    def fake_fetch(url, target, limit):
        calls.append(url)
        Path(target).write_bytes((digest + '  ' + installer.ASSET + '\n').encode() if url.endswith('.sha256') else content)
    monkeypatch.setattr(installer, 'fetch', fake_fetch)
    package, actual = installer.download('test.1', tmp_path)
    base = 'https://github.com/Lovest20018/LovestDNS/releases/download/vtest.1/'
    assert calls == [base + installer.ASSET, base + installer.ASSET + '.sha256']
    assert actual == digest and package.read_bytes() == content


@pytest.mark.parametrize('version', ['../other', 'other/tag', 'v1?redirect=other', '', 'a' * 121])
def test_invalid_release_version_never_starts_a_download(monkeypatch, tmp_path, version):
    monkeypatch.setattr(installer, 'fetch', lambda *args: pytest.fail('Invalid version reached the network'))
    with pytest.raises(ValueError, match='Invalid release version'):
        installer.download(version, tmp_path)
