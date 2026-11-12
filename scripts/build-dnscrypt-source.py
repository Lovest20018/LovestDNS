"""Build locked DNSCrypt candidates without profiling code or default integration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/dnscrypt-source.lock.json'
PATCHES = ROOT / 'packaging/components/patches'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_inputs(archive, toolchain, variants):
    lock = json.loads(LOCK.read_text())
    if not variants or len(set(variants)) != len(variants) or set(variants) - lock['variants'].keys():
        raise ValueError('Select distinct variants from the source lock')
    inputs = [(archive, lock['source']['sha256']), (toolchain, lock['toolchain']['sha256'])]
    names = {name for variant in variants for name in lock['variants'][variant]}
    inputs += [(PATCHES / name, lock['patches'][name]) for name in sorted(names)]
    for path, expected in inputs:
        if digest(path) != expected:
            raise ValueError('Locked input digest mismatch: ' + str(path))
    return lock


def build_fixture(go, work, env, lock):
    flags = ['build', '-trimpath', '-buildvcs=false', '-ldflags=-s -w -buildid=']
    for name, relative, manifest in (
            ('doh-fixture', 'doh', 'fixture-manifest.json'),
            ('paced-udp', 'udp-load', 'generator-manifest.json')):
        fixture = work / name
        source = ROOT / 'scripts/fixtures' / relative / 'main.go'
        subprocess.run([str(go), *flags, '-o', str(fixture), str(source)],
                       cwd=work, env=env, check=True, timeout=180)
        (work / manifest).write_text(json.dumps({
            'fixture_only': True, 'source_sha256': digest(source),
            'binary_sha256': digest(fixture), 'toolchain': lock['toolchain'],
            'build_flags': flags}, indent=2) + '\n')


def build(archive, toolchain, work, variants):
    archive, toolchain, work = archive.resolve(), toolchain.resolve(), work.resolve()
    lock = verified_inputs(archive, toolchain, variants)
    # Never extract into a previous source tree or reuse an instrumented build.
    work.mkdir(parents=True, exist_ok=False)
    with tarfile.open(toolchain) as src:
        src.extractall(work, filter='data')
    go = work / 'go/bin/go'
    env = {**os.environ, 'GOTOOLCHAIN': 'local', 'GOWORK': 'off', 'GOFLAGS': '',
           'GOEXPERIMENT': '', 'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': 'amd64',
           'GOAMD64': 'v1', 'GOMAXPROCS': '2', 'GOPROXY': 'off', 'GOSUMDB': 'off',
           'GOROOT': str(work / 'go')}
    actual = subprocess.check_output([str(go), 'version'], env=env, text=True).strip()
    if actual != f"go version {lock['toolchain']['version']} linux/amd64":
        raise ValueError('Unexpected source build toolchain: ' + actual)
    manifests = {}
    for variant in variants:
        target = work / variant
        target.mkdir()
        with tarfile.open(archive) as src:
            src.extractall(target, filter='data')
        source = target / ('dnscrypt-proxy-' + lock['version'])
        patches = {name: lock['patches'][name] for name in lock['variants'][variant]}
        for name in patches:
            with (target / (name + '.log')).open('w') as log:
                subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCHES / name)],
                               cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=10)
        if list(source.rglob('lovestdns_diagnostics.go')):
            raise ValueError('Diagnostic source cannot enter a normal candidate')
        binary = target / 'dnscrypt-proxy'
        flags = ['build', '-mod=vendor', '-trimpath', '-buildvcs=false', '-ldflags=-s -w -buildid=']
        with (target / 'build.log').open('w') as log:
            subprocess.run([str(go), *flags, '-o', str(binary), './dnscrypt-proxy'], cwd=source,
                           env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
        build_info = subprocess.check_output([str(go), 'version', '-m', str(binary)], env=env, text=True)
        (target / 'build-info.txt').write_text(build_info)
        manifest = {
            'candidate_only': True, 'diagnostic_only': False, 'release_approved': False,
            'version': lock['version'], 'variant': variant, 'architecture': lock['architecture'],
            'source': lock['source'], 'toolchain': lock['toolchain'],
            'build_flags': flags, 'build_environment': {key: env[key] for key in
                ('GOTOOLCHAIN', 'GOWORK', 'CGO_ENABLED', 'GOOS', 'GOARCH', 'GOAMD64', 'GOEXPERIMENT')},
            'patches_sha256': patches, 'binary_sha256': digest(binary),
            'source_lock_sha256': digest(LOCK), 'builder_sha256': digest(Path(__file__)),
            'limitations': ['Explicit benchmark candidate; not approved for the default component bundle.',
                            'CGO is disabled; this does not reproduce the deployed musl/PIE baseline build.']}
        (target / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        manifests[variant] = manifest
        print(json.dumps({'variant': variant, 'binary_sha256': manifest['binary_sha256']}), flush=True)
    (work / 'source-candidates.json').write_text(json.dumps(manifests, indent=2) + '\n')
    build_fixture(go, work, env, lock)
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--toolchain-archive', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--variants', default='upstream,buffer,udp')
    args = parser.parse_args()
    build(args.source_archive, args.toolchain_archive, args.work, args.variants.split(','))


if __name__ == '__main__':
    main()
