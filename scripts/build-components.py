"""Build checksum-locked, isolated amd64 DNS component payloads using Docker."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, default=ROOT / '.build/component-downloads')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('Output must not already exist')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    args.downloads.mkdir(parents=True, exist_ok=True)
    for entry in lock['components'].values():
        name = entry['url'].rsplit('/', 1)[1]
        archive = args.downloads / name
        if not archive.exists():
            temporary = archive.with_suffix(archive.suffix + '.part')
            request = urllib.request.Request(entry['url'], headers={'User-Agent': 'LovestDNS-component-builder/1'})
            try:
                with urllib.request.urlopen(request, timeout=60) as src, temporary.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['sha256']:
            raise SystemExit('Downloaded checksum mismatch: ' + name)
    with tempfile.TemporaryDirectory(prefix='lovestdns-components-') as temporary:
        work = Path(temporary)
        shutil.copytree(ROOT / 'packaging/components', work / 'packaging/components')
        shutil.copyfile(ROOT / 'packaging/components.lock.json', work / 'packaging/components.lock.json')
        (work / 'downloads').mkdir()
        for entry in lock['components'].values():
            name = entry['url'].rsplit('/', 1)[1]
            shutil.copyfile(args.downloads / name, work / 'downloads' / name)
        subprocess.run(['docker', 'build', '--platform=linux/amd64', '--target=bundle',
                        '--build-arg', 'BUILDER_IMAGE=' + lock['builder']['image'],
                        '--build-arg', 'BUILD_PACKAGES=' + ' '.join(lock['builder']['packages']),
                        '--output', 'type=local,dest=' + str(output),
                        '-f', str(work / 'packaging/components/Dockerfile'), str(work)], check=True)
    import runpy
    manifest = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](output, lock)
    print(json.dumps({'output': str(output), 'lock_sha256': manifest['lock_sha256'],
                      'versions': {k: v['version'] for k, v in manifest['components'].items()}}))


if __name__ == '__main__':
    main()
