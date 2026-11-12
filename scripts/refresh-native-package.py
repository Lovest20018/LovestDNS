"""Refresh app/UI in a verified native package, preserving its component bytes."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract(archive, target):
    modes, seen, roots = {}, set(), set()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if sum(item.size for item in members) > 512 * 1024 * 1024:
            raise ValueError('Native package exceeds refresh size limit')
        for item in members:
            path = PurePosixPath(item.name)
            if (path.is_absolute() or not path.parts or '..' in path.parts or '\\' in item.name
                    or str(path) in seen or not (item.isfile() or item.isdir())):
                raise ValueError('Unsafe native archive entry')
            seen.add(str(path)); roots.add(path.parts[0])
            modes['/'.join(path.parts[1:])] = item.mode & 0o777
        if len(roots) != 1:
            raise ValueError('Expected one native package root')
        source.extractall(target, filter='data')
    return target / roots.pop(), modes


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')


def preserve_provenance(package, archive_sha256):
    # Keep each immediate base's records under its verified archive identity.
    # Reusing base-* names collides on Windows and overwrites history on Unix.
    history = package / 'base-provenance' / archive_sha256
    history.mkdir(parents=True)
    for name in ('source-provenance.json', 'native-trial-provenance.json', 'native-refresh-provenance.json'):
        if (package / name).is_file():
            (package / name).rename(history / name)


def refresh(base, expected, frontend, output):
    base, frontend, output = map(Path, (base, frontend, output))
    if output.exists() or output.with_suffix(output.suffix + '.sha256').exists():
        raise ValueError('Use a new output filename')
    if not re.fullmatch(r'[a-f0-9]{64}', expected) or digest(base) != expected:
        raise ValueError('Base archive checksum mismatch')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise ValueError('Refresh requires clean committed source')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    source = runpy.run_path(str(ROOT / 'scripts/export-api-contract.py'))['source_summary']()
    builder = runpy.run_path(str(ROOT / 'scripts/build-native-package.py'))
    ui = builder['verify_frontend'](frontend, source)
    if ui['source_commit'] != commit:
        raise ValueError('Frontend and refresh must use the same source commit')
    installer = runpy.run_path(str(ROOT / 'packaging/native/install.py'))
    version = re.search(r'^VERSION = "([^"]+)"', (ROOT / 'integrated/backend/server.py').read_text(), re.M)[1]
    with tempfile.TemporaryDirectory(prefix='native-refresh-') as temporary:
        package, modes = extract(base, Path(temporary))
        old = installer['verify_package'](package)
        base_source = json.loads((package / 'source-provenance.json').read_text())
        for name in ('packaging/integrated/patch_upstream.py', 'packaging/native/adapt_upstream.py',
                     'packaging/native/requirements.lock.txt', 'packaging/components.lock.json', 'packaging/upstream.lock.json'):
            current = hashlib.sha256((ROOT / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            if base_source['files'].get(name) != current:
                raise ValueError('Inherited runtime inputs changed; require a full native build: ' + name)
        lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
        components = old.get('components') or {}
        if (set(components.get('selected', [])) != {'redis', 'unbound', 'dnscrypt'}
                or components.get('versions') != {name: value['version'] for name, value in lock['components'].items()}
                or old['files'].get('libexec/mosdns') != lock['frozen']['mosdns']['sha256']):
            raise ValueError('Base does not contain the selected reviewed component set')
        candidate = runpy.run_path(str(ROOT / 'packaging/components/dnscrypt_candidate.py'))['verify'](package / 'components/dnscrypt')
        if components.get('source_candidate', {}).get('binary_sha256') != candidate['binary_sha256']:
            raise ValueError('Base DNSCrypt component metadata differs from its binary')
        components = {**components, 'lock_sha256': components.get('lock_sha256') or components.get('bundle_lock_sha256')}
        bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))
        if components['lock_sha256'] != bundle['lock_digest'](lock):
            raise ValueError('Base component lock mismatch')
        inherited = {name: checksum for name, checksum in old['files'].items()
                     if name.startswith(('core/', 'lib/', 'libexec/', 'components/', 'wheels/'))}
        for relative, destination in (('integrated/runtime', 'app/runtime'), ('integrated/backend', 'app/backend'),
                                      ('packaging/native/runtime', 'app/native')):
            target = package / destination
            shutil.rmtree(target)
            shutil.copytree(ROOT / relative, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            for path in target.rglob('*.py'):
                path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
        shutil.rmtree(package / 'dist')
        shutil.copytree(frontend, package / 'dist')
        for relative, destination in (('web/backend/app.py', 'app/base/app.py'), ('packaging/native/install.py', 'install.py'),
                                      ('docs/lovestdns-native-deployment.md', 'README.md')):
            (package / destination).write_bytes((ROOT / relative).read_bytes().replace(b'\r\n', b'\n'))
        preserve_provenance(package, expected)
        write_json(package / 'source-provenance.json', source)
        provenance = {'format_version': 1, 'method': 'verified-components-with-current-app-ui', 'source_commit': commit,
                      'source_sha256': source['sha256'], 'base_archive_sha256': expected, 'base_version': old['version'],
                      'inherited_files': inherited, 'frontend_build_manifest_sha256': digest(frontend / 'build-manifest.json'),
                      'candidate_only': True, 'release_approved': False, 'performance_acceptance': False}
        info = json.loads((package / 'build-info.json').read_text())
        if 'native_trial' in info:
            info['base_native_trial'] = info.pop('native_trial')
        info.update(components=components, native_refresh={k: v for k, v in provenance.items() if k != 'inherited_files'})
        write_json(package / 'build-info.json', info)
        write_json(package / 'native-refresh-provenance.json', provenance)
        if any(digest(package / name) != checksum for name, checksum in inherited.items()):
            raise ValueError('Refresh modified an inherited component or dependency')
        files = {p.relative_to(package).as_posix(): digest(p) for p in sorted(package.rglob('*'))
                 if p.is_file() and p != package / 'manifest.json'}
        write_json(package / 'manifest.json', {**old, 'version': version, 'files': files, 'components': components,
                                              'installation_receipt_v1': True,
                                              'candidate_only': True, 'release_approved': False, 'performance_acceptance': False})
        installer['verify_package'](package)
        prefix = 'lovestdns-' + version
        def permissions(item):
            relative = str(PurePosixPath(item.name).relative_to(prefix))
            item.mode = 0o755 if item.isdir() else modes.get(relative, 0o644)
            item.uid = item.gid = 0; item.uname = item.gname = 'root'
            return item
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, 'x:gz') as archive:
            archive.add(package, arcname=prefix, filter=permissions)
    checksum = digest(output)
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n', newline='\n')
    return {'version': version, 'source_commit': commit, 'sha256': checksum, 'files': len(files),
            'inherited_files': len(inherited), 'components': components, 'package': str(output.resolve())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'frontend', 'output'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--base-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(refresh(args.base, args.base_sha256, args.frontend, args.output)))
