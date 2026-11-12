"""Create matched, explicit native installation trials from an immutable base archive."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
source = runpy.run_path(str(ROOT/'scripts/benchmark-dnscrypt-source.py'))
installer = runpy.run_path(str(ROOT/'packaging/native/install.py'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract_base(archive, target):
    with tarfile.open(archive) as src:
        seen, roots = set(), set()
        for entry in src.getmembers():
            name = PurePosixPath(entry.name)
            if (name.is_absolute() or not name.parts or '..' in name.parts or '\\' in entry.name
                    or not (entry.isfile() or entry.isdir()) or entry.name in seen):
                raise ValueError('Unsafe or duplicate native archive entry')
            seen.add(entry.name)
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError('Expected one native package root')
        src.extractall(target, filter='data')
    package = target/roots.pop()
    manifest = installer['verify_package'](package)
    if manifest.get('components') or (package/'components').exists():
        raise ValueError('Trial requires the original component baseline')
    lock = json.loads((ROOT/'packaging/components.lock.json').read_text())
    if manifest['files'].get('libexec/mosdns') != lock['frozen']['mosdns']['sha256']:
        raise ValueError('Frozen MosDNS differs from the reviewed baseline')
    required = ROOT/'packaging/native/requirements.lock.txt'
    if (package/'requirements.lock.txt').read_bytes().replace(b'\r\n', b'\n') != required.read_bytes().replace(b'\r\n', b'\n'):
        raise ValueError('Base wheel requirements differ; a fresh full build is required')
    return package, manifest


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2)+'\n', encoding='utf-8')


def refresh_app(package):
    overlay = {}
    for relative, destination in (('integrated/runtime','runtime'), ('integrated/backend','backend'),
                                  ('packaging/native/runtime','native')):
        target = package/'app'/destination
        shutil.rmtree(target)
        shutil.copytree(ROOT/relative, target, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for p in target.rglob('*'):
            if p.is_file(): overlay[f'app/{destination}/{p.relative_to(target).as_posix()}'] = digest(p)
    for relative, destination in (('web/backend/app.py','app/base/app.py'),
                                  ('packaging/native/install.py','install.py')):
        shutil.copyfile(ROOT/relative, package/destination)
        overlay[destination] = digest(package/destination)
    return overlay


def build(base_archive, base_sha256, candidate, output, version):
    if not re.fullmatch(r'[a-f0-9]{64}', base_sha256) or digest(base_archive) != base_sha256:
        raise ValueError('Base native archive SHA-256 mismatch')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
        raise ValueError('Invalid trial version')
    if output.exists():
        raise ValueError('Use a fresh output directory')
    binary, candidate_manifest = source['source_binary'](candidate, 'combined')
    lock = json.loads(source['LOCK'].read_text())
    if (candidate_manifest.get('architecture') != lock['architecture'] or lock['architecture'] != 'linux/amd64'
            or candidate_manifest.get('version') != lock['version']
            or candidate_manifest.get('build_environment',{}).get('CGO_ENABLED') != '0'):
        raise ValueError('Requires the normal static amd64 source candidate')
    with tempfile.TemporaryDirectory(prefix='lovestdns-native-trial-') as temporary:
        work = Path(temporary)
        base, original = extract_base(base_archive, work)
        original_files = dict(original['files'])
        overlay = refresh_app(base)
        # Frozen components, private libraries, templates, wheels and UI remain
        # byte-identical; only app code is refreshed equally for both trials.
        frozen = {name:checksum for name,checksum in original_files.items()
                  if name.startswith(('core/','lib/','libexec/','dist/','wheels/'))}
        if any(digest(base/name) != checksum for name,checksum in frozen.items()):
            raise ValueError('Shared trial refresh changed a frozen baseline file')
        output.mkdir(parents=True)
        outcomes = {}
        for label in ('control','combined'):
            package = work/('lovestdns-'+version+'.'+label)
            shutil.copytree(base, package)
            package_version = version+'.'+label
            server = package/'app/backend/server.py'
            text, count = re.subn(r'^VERSION = "[^"]+"', f'VERSION = "{package_version}"', server.read_text(), count=1, flags=re.M)
            if count != 1:
                raise ValueError('Missing backend version anchor')
            server.write_text(text)
            component = None
            if label == 'combined':
                payload = package/'components/dnscrypt'
                payload.mkdir(parents=True)
                shutil.copyfile(binary, payload/'dnscrypt-proxy')
                (payload/'dnscrypt-proxy').chmod(0o755)
                write_json(payload/'source-manifest.json', candidate_manifest)
                shutil.copyfile(source['LOCK'], payload/'source.lock.json')
                wrapper = package/'core/dnscrypt-proxy'
                wrapper.write_text('#!/bin/sh\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                                   'exec "$base/components/dnscrypt/dnscrypt-proxy" "$@"\n')
                wrapper.chmod(0o755)
                component = {'selected':['dnscrypt'],'versions':{'dnscrypt':candidate_manifest['version']},
                    'source_candidate':{'variant':'combined','binary_sha256':candidate_manifest['binary_sha256'],
                        'source_lock_sha256':candidate_manifest['source_lock_sha256'],
                        'patches_sha256':candidate_manifest['patches_sha256']},
                    'candidate_only':True,'release_approved':False}
            info = json.loads((package/'build-info.json').read_text())
            write_json(package/'build-info.json', {**info,'components':component,
                'native_trial':{'label':label,'base_archive_sha256':base_sha256,'release_approved':False}})
            write_json(package/'native-trial-provenance.json', {
                'format_version':1,'candidate_only':True,'release_approved':False,'performance_acceptance':False,
                'label':label,'base_archive_sha256':base_sha256,'base_version':original['version'],
                'base_manifest_sha256':digest(base/'manifest.json'),'builder_sha256':digest(Path(__file__)),
                'app_overlay_sha256':overlay,'backend_version':package_version,
                'limitations':['Matched installation trial; no default component promotion.',
                    'UI, wheels, templates and original component payloads come from the verified base archive.',
                    'Runtime, backend and installer are refreshed equally; this is not an unchanged historical release.']})
            files = {p.relative_to(package).as_posix():digest(p) for p in sorted(package.rglob('*')) if p.is_file() and p.name != 'manifest.json'}
            # Include nested component manifests in the package checksum table.
            for p in package.rglob('manifest.json'):
                if p.parent != package: files[p.relative_to(package).as_posix()] = digest(p)
            write_json(package/'manifest.json', {**original,'version':package_version,'files':files,'components':component,
                'candidate_only':True,'release_approved':False,'performance_acceptance':False})
            installer['verify_package'](package)
            changed = {name for name in frozen if files[name] != frozen[name]}
            if changed != ({'core/dnscrypt-proxy'} if label == 'combined' else set()):
                raise ValueError('Unexpected frozen trial changes')
            archive = output/(package.name+'.tar.gz')
            with tarfile.open(archive, 'x:gz') as dest:
                dest.add(package, arcname=package.name)
            checksum = digest(archive)
            archive.with_suffix('.gz.sha256').write_text(checksum+'  '+archive.name+'\n')
            outcomes[label] = {'archive':archive.name,'sha256':checksum,'version':package_version,
                'manifest_sha256':digest(package/'manifest.json'),'files':len(files),
                'dnscrypt_binary_sha256':candidate_manifest['binary_sha256'] if label == 'combined' else files['libexec/dnscrypt-proxy']}
        write_json(output/'trial-packages.json', {'base_archive_sha256':base_sha256,'packages':outcomes,
            'candidate_only':True,'release_approved':False,'performance_acceptance':False})
        return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-archive','candidate','output'): parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--baseline-sha256', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.baseline_archive,args.baseline_sha256,args.candidate,args.output,args.version)))


if __name__ == '__main__':
    main()
