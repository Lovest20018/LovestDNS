"""Build explicitly marked, matched container trials from an existing immutable local image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import runpy
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
source = runpy.run_path(str(ROOT/'scripts/benchmark-dnscrypt-source.py'))
FROZEN = ('/usr/sbin/mosdns','/usr/sbin/unbound','/usr/sbin/redis-server')
DOCKERFILE = '''FROM {base} AS common
COPY integrated/runtime/ /opt/suite/runtime/
COPY integrated/backend/ /opt/suite/backend/
COPY web/backend/app.py /app/app.py
RUN /opt/venv/bin/python -m compileall -q /opt/suite/runtime /opt/suite/backend /app/app.py
FROM common AS control
COPY control-info.json /opt/suite/build-info.json
FROM common AS combined
COPY dnscrypt-proxy /usr/bin/dnscrypt-proxy
COPY source-manifest.json source.lock.json /opt/suite/components/dnscrypt/
COPY combined-info.json /opt/suite/build-info.json
'''


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def docker(*args):
    return subprocess.check_output(['docker',*map(str,args)],text=True,timeout=180).strip()


def image_files(image):
    # No host mounts, network, ports or writable project volumes are used.
    code = ('import hashlib,json,pathlib; paths='+repr((*FROZEN,'/usr/bin/dnscrypt-proxy'))+
            '; print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in paths}))')
    return json.loads(docker('run','--rm','--network=none','--read-only','--cap-drop=ALL',
                            '--entrypoint=/opt/venv/bin/python',image,'-c',code))


def build(base_image, candidate, output, tag_prefix):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',base_image):
        raise ValueError('Use an immutable existing local image ID')
    if not re.fullmatch(r'lovestdns:dnstrial-[a-z0-9-]+',tag_prefix):
        raise ValueError('Use a marked lovestdns:dnstrial-* tag prefix')
    if output.exists(): raise ValueError('Output must be fresh')
    binary,manifest = source['source_binary'](candidate,'combined')
    lock = json.loads(source['LOCK'].read_text())
    if (manifest.get('architecture') != 'linux/amd64' or manifest.get('version') != lock['version']
            or manifest.get('build_environment',{}).get('CGO_ENABLED') != '0'):
        raise ValueError('Requires the reviewed static linux/amd64 source candidate')
    original = json.loads(docker('image','inspect',base_image))[0]
    if original['Id'] != base_image: raise ValueError('Base image identity mismatch')
    for label in ('base','control','combined'):
        if subprocess.run(['docker','image','inspect',tag_prefix+'-'+label],capture_output=True).returncode == 0:
            raise ValueError('Refusing to replace an existing image tag')
    before = image_files(base_image)
    lock = json.loads((ROOT/'packaging/components.lock.json').read_text())
    if before['/usr/sbin/mosdns'] != lock['frozen']['mosdns']['sha256']:
        raise ValueError('Base image does not contain the frozen customized MosDNS')
    info = json.loads(docker('run','--rm','--network=none','--read-only','--cap-drop=ALL',
                           '--entrypoint=/bin/cat',base_image,'/opt/suite/build-info.json'))
    if info.get('components',{}).get('selected'):
        raise ValueError('Use a baseline without independent component replacements')
    context = output/'context'
    context.mkdir(parents=True)
    for name in ('integrated/runtime','integrated/backend'):
        shutil.copytree(ROOT/name,context/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (context/'web/backend').mkdir(parents=True)
    shutil.copy2(ROOT/'web/backend/app.py',context/'web/backend/app.py')
    shutil.copy2(binary,context/'dnscrypt-proxy')
    (context/'dnscrypt-proxy').chmod(0o755)
    shutil.copy2(candidate/'source-manifest.json',context/'source-manifest.json')
    shutil.copy2(source['LOCK'],context/'source.lock.json')
    inputs = {p.relative_to(context).as_posix():digest(p) for p in sorted(context.rglob('*')) if p.is_file()}
    for label in ('control','combined'):
        metadata = {**info,'candidate_only':True,'release_approved':False,'performance_acceptance':False,
                    'trial':{'kind':'dnscrypt-container','case':label,'base_image_id':base_image,
                             'inputs_sha256':inputs,'frozen_binary_sha256':before}}
        if label == 'combined':
            metadata['components'] = {'selected':['dnscrypt'],'versions':{'dnscrypt':manifest['version']},
                                      'source_manifest':manifest,'source_lock_sha256':digest(source['LOCK'])}
        (context/(label+'-info.json')).write_text(json.dumps(metadata,indent=2)+'\n')
    # BuildKit parses a bare sha256:ID in FROM as a repository/tag. Give the
    # verified local image a fresh alias, then verify the alias before/after use.
    base_alias = tag_prefix+'-base'
    docker('image','tag',base_image,base_alias)
    if json.loads(docker('image','inspect',base_alias))[0]['Id'] != base_image:
        raise ValueError('Local base alias does not match the required image ID')
    (context/'Dockerfile').write_text(DOCKERFILE.format(base=base_alias))
    result = {'base_image_id':base_image,'base_alias':base_alias,'base_repo_digests':original.get('RepoDigests'),
              'inputs_sha256':inputs,'source_manifest':manifest,'images':{},'candidate_only':True,
              'performance_acceptance':False,'release_approved':False,'build_complete':False}
    def save(): (output/'trial-images.json').write_text(json.dumps(result,indent=2)+'\n')
    save()
    for label in ('control','combined'):
        tag = tag_prefix+'-'+label
        with (output/(label+'-build.log')).open('w') as log:
            subprocess.run(['docker','build','--pull=false','--network=none','--target',label,'-t',tag,str(context)],
                           check=True,text=True,stdout=log,stderr=subprocess.STDOUT,timeout=180)
        image_id = json.loads(docker('image','inspect',tag))[0]['Id']
        actual = image_files(image_id)
        expected = {**before,'/usr/bin/dnscrypt-proxy':manifest['binary_sha256']} if label == 'combined' else before
        if actual != expected: raise ValueError('Image has unexpected component bytes')
        result['images'][label] = {'tag':tag,'id':image_id,'binary_sha256':actual}
        save()
    if (image_files(base_image) != before
            or json.loads(docker('image','inspect',base_alias))[0]['Id'] != base_image):
        raise ValueError('Base image or alias changed')
    result['build_complete'] = True
    save()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image',required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tag-prefix',required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.base_image,args.candidate,args.output,args.tag_prefix)))


if __name__ == '__main__': main()
