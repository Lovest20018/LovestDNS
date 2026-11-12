"""Verify and install isolated DNS component payloads for both editions."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

NAMES = {'redis': {'redis-server', 'redis-cli'}, 'unbound': {'unbound', 'unbound-checkconf'},
         'dnscrypt': {'dnscrypt-proxy'}}


def lock_digest(lock):
    return hashlib.sha256(json.dumps(lock, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def relative(value):
    if not isinstance(value, str):
        raise ValueError('Unsafe component path')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(p in {'.', '..', ''} for p in value.split('/')) or '\\' in value:
        raise ValueError('Unsafe component path')
    return path


def verify(root, lock):
    root = Path(root)
    if root.is_symlink():
        raise ValueError('Component bundle must be a regular directory')
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('format_version') != 1 or manifest.get('architecture') != 'amd64' or manifest.get('lock_sha256') != lock_digest(lock):
        raise ValueError('Component bundle does not match the locked amd64 build')
    declared = manifest.get('files')
    if not isinstance(declared, dict) or not declared:
        raise ValueError('Missing component file manifest')
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Component bundle symlinks are forbidden')
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(declared) | {'manifest.json'}:
        raise ValueError('Unexpected or missing component payload')
    for name, digest in declared.items():
        path = root.joinpath(*relative(name).parts)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Component payload checksum mismatch: ' + name)
    components = manifest.get('components', {})
    if set(components) != set(NAMES):
        raise ValueError('Incomplete component bundle')
    for name, binaries in NAMES.items():
        entry = components[name]
        if entry.get('version') != lock['components'][name]['version'] or set(entry.get('binaries', {})) != binaries:
            raise ValueError('Invalid component metadata: ' + name)
        runtime = entry.get('runtime', {})
        if not isinstance(runtime, dict) or set(runtime) not in (set(), {'loader', 'library_path'}):
            raise ValueError('Invalid component runtime')
        paths = list(entry['binaries'].values())
        if runtime.get('loader'):
            paths.append(runtime['loader'])
            if runtime['library_path'] != 'lib' or runtime['loader'] != 'lib/ld-musl-x86_64.so.1':
                raise ValueError('Unexpected component runtime path')
        for value in paths:
            if not isinstance(value, str) or not all(c.isalnum() or c in '/._-' for c in value):
                raise ValueError('Unexpected component executable path')
            key = name + '/' + relative(value).as_posix()
            if key not in declared:
                raise ValueError('Missing declared component executable: ' + key)
    return manifest


def wrapper(component, binary, entry, *, container=False):
    relative(entry['binaries'][binary])
    base = f'/opt/suite/components/{component}' if container else f'$base/components/{component}'
    prefix = '#!/bin/sh\n'
    if not container:
        prefix += 'base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
    runtime = entry['runtime']
    program = f'"{base}/{entry["binaries"][binary]}"'
    if runtime.get('loader'):
        program = f'"{base}/{runtime["loader"]}" --library-path "{base}/{runtime["library_path"]}" ' + program
    return prefix + 'exec ' + program + ' "$@"\n'


def install(source, target, lock, selected=None, *, container=False):
    manifest = verify(source, lock)
    selected = sorted(NAMES if selected is None else selected)
    if not selected or set(selected) - set(NAMES):
        raise ValueError('Unknown or empty component selection')
    source, target = Path(source), Path(target)
    payload = target / ('opt/suite/components' if container else 'components')
    launch = target / ('usr/sbin' if container else 'core')
    launch.mkdir(parents=True, exist_ok=True)
    for name in selected:
        shutil.copytree(source / name, payload / name)
        runtime = manifest['components'][name]['runtime']
        if runtime.get('loader'):
            (payload / name / runtime['loader']).chmod(0o755)
        for binary in NAMES[name]:
            path = launch / binary
            # A container may originally use /usr/bin for DNSCrypt. Replace both
            # locations so existing orchestration keeps its exact command path.
            if path.is_symlink():
                path.unlink()
            path.write_text(wrapper(name, binary, manifest['components'][name], container=container))
            path.chmod(0o755)
            executable = payload / name / manifest['components'][name]['binaries'][binary]
            executable.chmod(0o755)
            if container and binary == 'dnscrypt-proxy':
                other = target / 'usr/bin/dnscrypt-proxy'
                if other.is_symlink():
                    other.unlink()
                shutil.copyfile(path, other)
                other.chmod(0o755)
    payload.mkdir(parents=True, exist_ok=True)
    (payload / 'manifest.json').write_text(json.dumps({**manifest, 'selected': selected}, sort_keys=True, indent=2) + '\n')
    return {'selected': selected, 'lock_sha256': manifest['lock_sha256'],
            'versions': {name: manifest['components'][name]['version'] for name in selected}}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--target', type=Path, default=Path('/'))
    parser.add_argument('--select', default='redis,unbound,dnscrypt')
    args = parser.parse_args()
    print(json.dumps(install(args.bundle, args.target, json.loads(args.lock.read_text()), args.select.split(','), container=True)))
