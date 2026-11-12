"""Build an offline Debian amd64 package; Docker is a build tool only."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def verify_frontend(directory, source):
    """Reject development or stale assets before starting an expensive build."""
    directory = Path(directory)
    try:
        manifest = json.loads((directory / 'build-manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError('Frontend build manifest is missing or invalid') from error
    if (manifest.get('schema_version') != 1 or manifest.get('dependency_mode') != 'npm-ci'
            or manifest.get('working_tree_modified') is not False
            or not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('source_commit', '')))):
        raise ValueError('Frontend must be built with npm ci from a clean committed source')
    if manifest.get('source_snapshot') != source:
        raise ValueError('Frontend source differs from the package source')
    lock = ROOT / 'integrated/frontend/package-lock.json'
    if manifest.get('lock_sha256') != hashlib.sha256(lock.read_bytes()).hexdigest():
        raise ValueError('Frontend dependency lock differs from the package lock')
    artifacts = manifest.get('artifacts')
    files = {p.relative_to(directory).as_posix(): p for p in directory.rglob('*') if p.is_file()}
    if not isinstance(artifacts, dict) or not artifacts or set(files) != set(artifacts) | {'build-manifest.json'}:
        raise ValueError('Frontend artifact list differs from its manifest')
    if any(p.is_symlink() for p in directory.rglob('*')) or any(
            hashlib.sha256(files[name].read_bytes()).hexdigest() != digest for name, digest in artifacts.items()):
        raise ValueError('Frontend artifact checksum differs from its manifest')
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend-dist', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default=None)
    parser.add_argument('--components-bundle', type=Path, help='Verified isolated component bundle; omitted only for upstream baseline builds')
    parser.add_argument('--components', default='redis,unbound,dnscrypt', help='Components to replace for controlled single-component trials')
    parser.add_argument('--dnscrypt-source-candidate', type=Path, help='Explicit reviewed normal combined candidate; never used by default')
    args = parser.parse_args()
    version = args.version or re.search(r'^VERSION = "([^"]+)"', (ROOT / 'integrated/backend/server.py').read_text(), re.M)[1]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', version):
        raise SystemExit('Invalid version')
    source_provenance = runpy.run_path(str(ROOT / 'scripts/export-api-contract.py'))['source_summary']()
    frontend = verify_frontend(args.frontend_dist, source_provenance)
    candidate_installer = None
    if args.dnscrypt_source_candidate:
        if not args.components_bundle or 'dnscrypt' not in args.components.split(','):
            raise SystemExit('DNSCrypt source candidate requires an explicit bundle including dnscrypt')
        candidate_installer = load('dnscrypt_candidate', ROOT / 'packaging/components/dnscrypt_candidate.py')
        candidate_installer.verify(args.dnscrypt_source_candidate)
    lock = json.loads((ROOT / 'packaging/upstream.lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='lovestdns-build-') as temporary:
        work = Path(temporary)
        package = work / f'lovestdns-{version}'
        core, libs, binaries = (package / p for p in ('core', 'lib', 'libexec'))
        for path in (core, libs, binaries):
            path.mkdir(parents=True)
        container = subprocess.check_output(['docker', 'create', '--entrypoint', '/bin/true', lock['image']], text=True).strip()
        try:
            def copy(source, target):
                run(['docker', 'cp', '-L', container + ':' + source, str(target)])
            for name in ('init.sh', 'watch_list.sh', 'reload.sh', 'data_update.sh', 'unbound.conf', 'redis.conf',
                         'mosdns.yaml', 'dnscrypt.toml', 'custom_env.ini', 'custom_mod.yaml', 'unbound_custom.conf',
                         'force_dnscrypt_list.txt', 'force_recurse_list.txt', 'force_forward_list.txt',
                         'Country-only-cn-private.mmdb.xz', 'trackerslist.txt.xz', 'global_mark.dat', 'dnscrypt-resolvers'):
                copy('/usr/sbin/' + name, core / name)
            copy('/etc/unbound/named.cache', core / 'named.cache')
            for name in ('mosdns', 'unbound', 'unbound-checkconf', 'redis-server', 'redis-cli'):
                copy('/usr/sbin/' + name, binaries / name)
            copy('/usr/bin/dnscrypt-proxy', binaries / 'dnscrypt-proxy')
            for source, name in (('/lib/ld-musl-x86_64.so.1', 'ld-musl-x86_64.so.1'),
                                 ('/lib/libssl.so.3', 'libssl.so.3'), ('/lib/libcrypto.so.3', 'libcrypto.so.3'),
                                 ('/usr/lib/libevent-2.1.so.7', 'libevent-2.1.so.7'),
                                 ('/usr/lib/libhiredis.so.1.1.0', 'libhiredis.so.1.1.0')):
                copy(source, libs / name)
        finally:
            run(['docker', 'rm', '-v', container], stdout=subprocess.DEVNULL)
        # The original binaries remain unchanged; record their exact bytes.
        origin = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for d in (core, libs, binaries) for p in d.rglob('*') if p.is_file()}
        component_lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
        if origin['libexec/mosdns'] != component_lock['frozen']['mosdns']['sha256']:
            raise SystemExit('Frozen MosDNS checksum differs from the reviewed baseline')
        patcher = load('patch_upstream', ROOT / 'packaging/integrated/patch_upstream.py')
        patcher.patch(core, lock)
        load('adapt_upstream', ROOT / 'packaging/native/adapt_upstream.py').adapt(core)
        for binary in binaries.iterdir():
            binary.chmod(0o755)
            wrapper = core / binary.name
            wrapper.write_text('#!/bin/sh\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                               f'exec "$base/lib/ld-musl-x86_64.so.1" --library-path "$base/lib" "$base/libexec/{binary.name}" "$@"\n')
            wrapper.chmod(0o755)
        # The musl runtime is private to these binaries, never installed over
        # Debian's libc. The payload contains no Alpine root filesystem.
        shutil.copyfile(libs / 'ld-musl-x86_64.so.1', libs / 'libc.musl-x86_64.so.1')
        components = None
        if args.components_bundle:
            components = load('component_bundle', ROOT / 'packaging/components/bundle.py').install(
                args.components_bundle, package, component_lock, args.components.split(','))
        if candidate_installer:
            components = candidate_installer.install(args.dnscrypt_source_candidate, package, components)
        app = package / 'app'
        shutil.copytree(ROOT / 'integrated/runtime', app / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'integrated/backend', app / 'backend', ignore=shutil.ignore_patterns('__pycache__'))
        server = app / 'backend/server.py'
        server.write_text(re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{version}"', server.read_text(), count=1, flags=re.M))
        shutil.copytree(ROOT / 'packaging/native/runtime', app / 'native', ignore=shutil.ignore_patterns('__pycache__'))
        (app / 'base').mkdir()
        shutil.copyfile(ROOT / 'web/backend/app.py', app / 'base/app.py')
        shutil.copytree(args.frontend_dist, package / 'dist')
        shutil.copyfile(ROOT / 'packaging/native/install.py', package / 'install.py')
        shutil.copyfile(ROOT / 'packaging/native/requirements.lock.txt', package / 'requirements.lock.txt')
        (package / 'build-info.json').write_text(json.dumps({**lock, 'components': components,
            'application': {'source_commit': frontend['source_commit'], 'source_sha256': source_provenance['sha256'],
                            'method': 'native-source-build'}}, sort_keys=True, indent=2) + '\n')
        shutil.copyfile(ROOT / 'packaging/components.lock.json', package / 'components.lock.json')
        shutil.copyfile(ROOT / 'docs/lovestdns-native-deployment.md', package / 'README.md')
        shutil.copyfile(ROOT / '.env.native.example', package / 'native.env.example')
        shutil.copyfile(ROOT / 'LICENSE', package / 'LICENSE')
        (package / 'source-provenance.json').write_text(json.dumps(source_provenance, sort_keys=True, indent=2) + '\n')
        wheels = package / 'wheels'
        wheels.mkdir()
        # Debian 12 and 13 use CPython 3.11 and 3.13 respectively. Fetch both
        # wheel ABIs so installation needs no PyPI connection or compiler.
        for py in ('311', '313'):
            run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--platform', 'manylinux2014_x86_64',
                 '--python-version', py, '--implementation', 'cp', '--abi', 'cp' + py,
                 '-r', str(package / 'requirements.lock.txt'), '-d', str(wheels)])
        for script in core.glob('*.sh'):
            run(['bash', '-n', str(script)])
        files = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(package.rglob('*')) if p.is_file()}
        manifest = {'format_version': 1, 'product': 'LovestDNS', 'deployment': 'native', 'version': version,
                    'installation_receipt_v1': True,
                    'architecture': 'amd64', 'debian': ['12', '13'], 'upstream': lock,
                    'original_upstream_files': origin, 'components': components, 'files': files,
                    'runtime': 'systemd + Debian Python; frozen MosDNS runtime and isolated optional DNS component runtimes; no Docker daemon or chroot'}
        if candidate_installer:
            manifest.update(candidate_only=True, release_approved=False, performance_acceptance=False)
        (package / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(args.output, 'w:gz') as archive:
            archive.add(package, arcname=package.name)
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
        args.output.with_suffix(args.output.suffix + '.sha256').write_text(digest + '  ' + args.output.name + '\n')
        print(json.dumps({'package': str(args.output.resolve()), 'sha256': digest, 'version': version, 'files': len(files)}))


if __name__ == '__main__':
    main()
