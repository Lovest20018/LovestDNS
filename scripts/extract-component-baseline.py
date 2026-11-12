"""Extract locked original DNS executables for isolated cross-version tests."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be a new directory')
    lock = json.loads((ROOT / 'packaging/upstream.lock.json').read_text())
    component_lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    container = subprocess.check_output(['docker', 'create', '--entrypoint=/bin/true', lock['image']], text=True).strip()
    try:
        for directory in ('core', 'libexec', 'lib'):
            (args.output / directory).mkdir(parents=True)
        for binary in ('mosdns', 'redis-server', 'redis-cli', 'unbound', 'unbound-checkconf', 'dnscrypt-proxy'):
            source = ('/usr/bin/' if binary == 'dnscrypt-proxy' else '/usr/sbin/') + binary
            destination = args.output / 'libexec' / binary
            subprocess.run(['docker', 'cp', '-L', container + ':' + source, str(destination)], check=True)
            destination.chmod(0o755)
            launcher = args.output / 'core' / binary
            launcher.write_text('#!/bin/sh\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                                f'exec "$base/lib/ld-musl-x86_64.so.1" --library-path "$base/lib" "$base/libexec/{binary}" "$@"\n')
            launcher.chmod(0o755)
        for source in ('/lib/ld-musl-x86_64.so.1', '/lib/libssl.so.3', '/lib/libcrypto.so.3',
                       '/usr/lib/libevent-2.1.so.7', '/usr/lib/libhiredis.so.1.1.0'):
            destination = args.output / 'lib' / Path(source).name
            subprocess.run(['docker', 'cp', '-L', container + ':' + source, str(destination)], check=True)
            destination.chmod(0o755)
        shutil.copyfile(args.output / 'lib/ld-musl-x86_64.so.1', args.output / 'lib/libc.musl-x86_64.so.1')
        if hashlib.sha256((args.output / 'libexec/mosdns').read_bytes()).hexdigest() != component_lock['frozen']['mosdns']['sha256']:
            raise ValueError('Frozen MosDNS differs from reviewed baseline')
        print('Locked upstream baseline extracted')
    finally:
        subprocess.run(['docker', 'rm', '-v', container], check=True, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
