"""Build in the pinned Alpine container, retaining private dependency closures."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

ROOT = Path('/build')
LOCK = json.loads((ROOT / 'components.lock.json').read_text())
OUT = ROOT / 'output'
OUT.mkdir()


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def capture(args):
    return subprocess.check_output(args, text=True).strip()


def extract(name):
    entry = LOCK['components'][name]
    archive = ROOT / 'downloads' / entry['url'].rsplit('/', 1)[1]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('Source checksum mismatch: ' + name)
    target = ROOT / ('source-' + name)
    target.mkdir()
    with tarfile.open(archive) as source:
        # Public release archives contain symlinks; data_filter bounds links and
        # paths to this fresh build-only extraction directory.
        source.extractall(target, filter='data')
    folders = [p for p in target.iterdir() if p.is_dir()]
    if len(folders) != 1:
        raise ValueError('Unexpected source archive layout')
    return folders[0]


def closure(binary, folder):
    libs = folder / 'lib'
    libs.mkdir(exist_ok=True)
    header = capture(['readelf', '-l', str(binary)])
    interpreter = re.search(r'Requesting program interpreter: ([^\]]+)', header)
    if not interpreter:
        return {}
    loader = Path(interpreter[1])
    if loader.name != 'ld-musl-x86_64.so.1':
        raise ValueError('Unexpected component ELF interpreter')
    shutil.copyfile(loader, libs / loader.name)
    (libs / loader.name).chmod(0o755)
    pending = [binary]
    copied = {loader.name}
    while pending:
        current = pending.pop()
        needed = re.findall(r'\(NEEDED\).*\[([^\]]+)\]', capture(['readelf', '-d', str(current)]))
        for name in needed:
            if name in copied:
                continue
            if '/' in name:
                raise ValueError('Unexpected dependency path')
            source = next((p / name for p in (Path('/lib'), Path('/usr/lib')) if (p / name).exists()), None)
            if source is None:
                raise ValueError('Missing dependency: ' + name)
            target = libs / name
            shutil.copyfile(source, target)
            copied.add(name)
            pending.append(target)
    return {'loader': 'lib/' + loader.name, 'library_path': 'lib'}


metadata = {}
for name, entry in LOCK['components'].items():
    source = extract(name)
    folder = OUT / name
    (folder / 'bin').mkdir(parents=True)
    if name == 'redis':
        run(['make', '-j2', 'MALLOC=libc', 'BUILD_TLS=no', 'redis-server', 'redis-cli'], cwd=source)
        binaries = {x: source / 'src' / x for x in entry['binaries']}
    elif name == 'unbound':
        run(['./configure', *entry['configure']], cwd=source)
        run(['make', '-j2', 'unbound', 'unbound-checkconf'], cwd=source)
        binaries = {x: source / x for x in entry['binaries']}
    else:
        binaries = {'dnscrypt-proxy': source / 'dnscrypt-proxy'}
    runtime = None
    for binary, path in binaries.items():
        target = folder / 'bin' / binary
        shutil.copyfile(path, target)
        target.chmod(0o755)
        this_runtime = closure(target, folder)
        if runtime is not None and runtime != this_runtime:
            raise ValueError('Mismatched executable runtime')
        runtime = this_runtime
    licenses = folder / 'licenses'
    licenses.mkdir()
    for path in source.iterdir():
        if path.is_file() and re.match(r'(?i)(license|copying|copyright)', path.name):
            shutil.copyfile(path, licenses / path.name)
    metadata[name] = {'version': entry['version'], 'source': entry, 'runtime': runtime,
                      'binaries': {x: 'bin/' + x for x in binaries}}

# Preserve dependency licensing information distributed by Alpine.
run(['apk', 'info', '-v'])
inventory = capture(['apk', 'info', '-v']).splitlines()
(OUT / 'build-info.json').write_text(json.dumps({'builder': LOCK['builder'], 'packages': inventory,
    'licenses': capture(['apk', 'info', '--license']),
    'compiler': capture(['gcc', '--version']).splitlines()[0]}, sort_keys=True, indent=2) + '\n')
files = {p.relative_to(OUT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(OUT.rglob('*')) if p.is_file()}
digest = hashlib.sha256(json.dumps(LOCK, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
(OUT / 'manifest.json').write_text(json.dumps({'format_version': 1, 'architecture': 'amd64',
    'lock_sha256': digest, 'components': metadata, 'files': files}, sort_keys=True, indent=2) + '\n')
