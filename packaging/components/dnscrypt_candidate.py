"""Explicit native packaging of the reviewed, non-instrumented DNSCrypt candidate."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify(source):
    source = Path(source)
    lock_path = ROOT / 'packaging/dnscrypt-source.lock.json'
    lock = json.loads(lock_path.read_text())
    manifest_path, binary = source / 'source-manifest.json', source / 'dnscrypt-proxy'
    if source.is_symlink() or manifest_path.is_symlink() or binary.is_symlink():
        raise ValueError('DNSCrypt candidate inputs must be regular files')
    manifest = json.loads(manifest_path.read_text())
    patches = {name: lock['patches'][name] for name in lock['variants']['combined']}
    if (manifest.get('variant') != 'combined' or manifest.get('candidate_only') is not True
            or manifest.get('diagnostic_only') is not False or manifest.get('release_approved') is not False
            or manifest.get('version') != lock['version'] or manifest.get('architecture') != 'linux/amd64'
            or manifest.get('build_environment', {}).get('CGO_ENABLED') != '0'):
        raise ValueError('Requires the normal static amd64 combined DNSCrypt candidate')
    if (manifest.get('source_lock_sha256') != digest(lock_path) or manifest.get('patches_sha256') != patches
            or manifest.get('source') != lock['source'] or manifest.get('toolchain') != lock['toolchain']
            or any(digest(ROOT / 'packaging/components/patches' / name) != value for name, value in patches.items())):
        raise ValueError('DNSCrypt candidate differs from the reviewed source lock or patches')
    if digest(binary) != manifest.get('binary_sha256'):
        raise ValueError('DNSCrypt candidate binary checksum mismatch')
    with binary.open('rb') as stream:
        header = stream.read(20)
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
        raise ValueError('DNSCrypt candidate must contain an amd64 ELF executable')
    return manifest


def install(source, package, components):
    manifest = verify(source)
    if not components or 'dnscrypt' not in components['selected']:
        raise ValueError('Select DNSCrypt from a verified component bundle before adding the candidate')
    source, package = Path(source), Path(package)
    directory = package / 'components'
    installed = json.loads((directory / 'manifest.json').read_text())
    entry = installed['components']['dnscrypt']
    if entry['binaries'] != {'dnscrypt-proxy': 'bin/dnscrypt-proxy'} or entry['runtime']:
        raise ValueError('Unexpected baseline DNSCrypt layout')
    # Retain the unmodified input declaration, then describe the actual payload.
    shutil.copyfile(directory / 'manifest.json', directory / 'bundle-input-manifest.json')
    binary = directory / 'dnscrypt/bin/dnscrypt-proxy'
    shutil.copyfile(source / 'dnscrypt-proxy', binary)
    if digest(binary) != manifest['binary_sha256']:
        raise ValueError('DNSCrypt input changed during packaging')
    binary.chmod(0o755)
    (directory / 'dnscrypt/source-manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    shutil.copyfile(ROOT / 'packaging/dnscrypt-source.lock.json', directory / 'dnscrypt/source.lock.json')
    candidate = {key: manifest[key] for key in ('variant', 'binary_sha256', 'source_lock_sha256', 'patches_sha256')}
    entry.update(source=manifest['source'], source_candidate=candidate)
    installed.update(format_version=2, kind='native-selected-component-payload',
                     candidate_only=True, release_approved=False, performance_acceptance=False)
    installed['components'] = {name: value for name, value in installed['components'].items() if name in components['selected']}
    installed['files'] = {p.relative_to(directory).as_posix(): digest(p) for p in sorted(directory.rglob('*'))
                          if p.is_file() and p != directory / 'manifest.json'}
    (directory / 'manifest.json').write_text(json.dumps(installed, sort_keys=True, indent=2) + '\n')
    return {**components, 'source_candidate': candidate, 'candidate_only': True,
            'release_approved': False, 'performance_acceptance': False}
