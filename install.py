#!/usr/bin/env python3
"""Download, verify and install a LovestDNS Debian native release."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = 'Lovest20018/LovestDNS'
DEFAULT_VERSION = '0.1.0-dev.lovestdns-native.20261104.11'
ASSET = 'lovestdns-native-debian-amd64.tar.gz'
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXTRACTED = 768 * 1024 * 1024


def fetch(url, target, limit):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'api.github.com', 'github.com'}:
        raise ValueError('Release downloads must use GitHub HTTPS')
    request = urllib.request.Request(url, headers={'User-Agent': 'LovestDNS-installer',
                                                  'Accept': 'application/octet-stream'})
    with urllib.request.urlopen(request, timeout=60) as response, Path(target).open('wb') as output:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError('Release download exceeds its size limit')
            output.write(chunk)


def download(version, directory):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', version):
        raise ValueError('Invalid release version')
    directory = Path(directory)
    tag = 'v' + version
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    for name, limit in ((ASSET, MAX_ARCHIVE), (ASSET + '.sha256', 4096)):
        # Exact public asset URLs avoid anonymous GitHub API rate limits.
        fetch(base + name, directory / name, limit)
    checksum = (directory / (ASSET + '.sha256')).read_text().strip()
    match = re.fullmatch(r'([a-f0-9]{64})  ' + re.escape(ASSET), checksum)
    if not match:
        raise ValueError('Expected exactly one SHA-256 entry for the native package')
    return directory / ASSET, match[1]


def unpack(package, digest, directory):
    package, directory = Path(package), Path(directory)
    if package.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Native package exceeds its size limit')
    with package.open('rb') as source:
        actual_digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if not re.fullmatch(r'[a-f0-9]{64}', digest) or actual_digest != digest:
        raise ValueError('Native package SHA-256 differs; nothing was installed')
    with tarfile.open(package, 'r:gz') as archive:
        seen, roots, size = set(), set(), 0
        members = archive.getmembers()
        if len(members) > 4096:
            raise ValueError('Too many native package entries')
        for member in members:
            path = PurePosixPath(member.name)
            canonical = path.as_posix()
            if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in member.name or ':' in member.name
                    or canonical in seen or not (member.isfile() or member.isdir())
                    or member.mode & 0o7000):
                raise ValueError('Unsafe native package entry: ' + member.name)
            seen.add(canonical)
            roots.add(path.parts[0])
            size += member.size
            if size > MAX_EXTRACTED:
                raise ValueError('Extracted package exceeds its size limit')
        if len(roots) != 1 or not re.fullmatch(r'lovestdns-[A-Za-z0-9_.-]+', next(iter(roots))):
            raise ValueError('Expected one native package directory')
        archive.extractall(directory, filter='data')
    root = directory / roots.pop()
    manifest = json.loads((root / 'manifest.json').read_text())
    if (manifest.get('product'), manifest.get('deployment'), manifest.get('architecture')) != ('LovestDNS', 'native', 'amd64'):
        raise ValueError('Unexpected native package identity')
    files = manifest['files']
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()
              and path != root / 'manifest.json'}
    if set(files) != actual or 'install.py' not in files:
        raise ValueError('Unexpected native package file list')
    for name, expected in files.items():
        path = root / name
        if root not in path.resolve().parents or path.is_symlink() or not path.is_file():
            raise ValueError('Unsafe native package payload')
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != expected:
                raise ValueError('Native payload SHA-256 differs: ' + name)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default=DEFAULT_VERSION, help='Exact published version without the v prefix')
    parser.add_argument('--package', type=Path, help='Use an existing package instead of downloading')
    parser.add_argument('--sha256', help='Required trusted digest with --package')
    parser.add_argument('--config', type=Path, help='Private host configuration; existing settings are preserved when omitted')
    parser.add_argument('--instance', default='', help='Optional isolated instance name')
    parser.add_argument('--import-data', type=Path, help='Consistent offline data copy for first installation')
    parser.add_argument('--download-only', type=Path, metavar='DIRECTORY', help='Verify and save the package without installing')
    args = parser.parse_args()
    if bool(args.package) != bool(args.sha256):
        parser.error('--package and --sha256 must be supplied together')
    if args.instance and not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', args.instance):
        parser.error('Invalid instance name')
    if not args.download_only and (not hasattr(os, 'geteuid') or os.geteuid() != 0):
        parser.error('Run installation as root or through sudo')
    with tempfile.TemporaryDirectory(prefix='lovestdns-release-') as temporary:
        work = Path(temporary)
        package, digest = (args.package.resolve(), args.sha256) if args.package else download(args.version, work)
        root = unpack(package, digest, work / 'payload')
        if not args.package and json.loads((root / 'manifest.json').read_text())['version'] != args.version:
            raise ValueError('Package version differs from the selected release')
        if args.download_only:
            args.download_only.mkdir(parents=True, exist_ok=True)
            target = args.download_only / ASSET
            if target.exists() or target.with_name(ASSET + '.sha256').exists():
                raise ValueError('Download target already exists')
            shutil.copyfile(package, target)
            target.with_name(ASSET + '.sha256').write_bytes((digest + '  ' + ASSET + '\n').encode('ascii'))
            print('Verified package: ' + str(target.resolve()))
            return
        command = [sys.executable, str(root / 'install.py'), '--instance', args.instance, 'install']
        for flag, value in (('--config', args.config), ('--import-data', args.import_data)):
            if value:
                command.extend([flag, str(value.resolve(strict=True))])
        subprocess.run(command, check=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('Installation failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
