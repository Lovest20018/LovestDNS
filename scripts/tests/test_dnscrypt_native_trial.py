"""Verify trial archives preserve the base and reject unbound or unsafe inputs."""
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
trial = runpy.run_path(str(ROOT/'scripts/build-dnscrypt-native-trial.py'))
native_trial = runpy.run_path(str(ROOT/'scripts/test-dnscrypt-native-trial.py'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def test_native_trial_resources_can_target_the_debian12_host():
    assert native_trial['resource_limits']('2-3',125,768,{0,1,2,3}) == {
        'allowed_cpus':'2-3','cpu_quota_percent':125,'memory_max_mb':768}
    assert native_trial['resource_limits']('0,2-3',50,512,{0,1,2,3})['allowed_cpus']=='0,2-3'


@pytest.mark.parametrize('cpus,quota,memory',[
    ('4-7',125,768),('3-2',125,768),('2-3\nEnvironment=BAD=1',125,768),
    ('',125,768),('0-999999999',125,768),('2-3',0,768),('2-3',301,768),
    ('2-3',125,128),('2-3',125,4096)])
def test_native_trial_rejects_invalid_or_unavailable_resource_limits(cpus,quota,memory):
    with pytest.raises(ValueError): native_trial['resource_limits'](cpus,quota,memory,{0,1,2,3})


def test_trial_extraction_needs_no_tar_filter_api(tmp_path,monkeypatch):
    archive=tmp_path/'package.tar'
    with tarfile.open(archive,'w') as z:
        entry=tarfile.TarInfo('package/core/tool');entry.size=7;entry.mode=0o755
        z.addfile(entry,io.BytesIO(b'payload'))
    target=tmp_path/'unpack';target.mkdir()
    def forbidden(*args,**kwargs):raise AssertionError('Do not depend on TarFile.extractall')
    monkeypatch.setattr(tarfile.TarFile,'extractall',forbidden)
    native_trial['extract_package'](archive,target)
    assert (target/'package/core/tool').read_bytes()==b'payload'
    if sys.platform!='win32':assert (target/'package/core/tool').stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('fault',['traversal','absolute','backslash','symlink','hardlink','device','duplicate','normalized-duplicate','two-roots','file-parent'])
def test_trial_extraction_rejects_unsafe_input_before_writing(tmp_path,fault):
    archive=tmp_path/'unsafe.tar';target=tmp_path/'target';target.mkdir()
    with tarfile.open(archive,'w') as z:
        entry=tarfile.TarInfo('root/file');entry.size=1;z.addfile(entry,io.BytesIO(b'x'))
        bad=tarfile.TarInfo({'traversal':'root/../escape','absolute':'/escape','backslash':'root\\escape',
            'normalized-duplicate':'root/./file','two-roots':'other/file','file-parent':'root/file/child'}.get(fault,'root/file'))
        if fault in ('symlink','hardlink','device'):
            bad.name='root/link';bad.type={'symlink':tarfile.SYMTYPE,'hardlink':tarfile.LNKTYPE,'device':tarfile.CHRTYPE}[fault]
            bad.linkname='/etc/passwd';z.addfile(bad)
        else:bad.size=1;z.addfile(bad,io.BytesIO(b'y'))
    with pytest.raises(ValueError,match='Unsafe|Expected one'):native_trial['extract_package'](archive,target)
    assert not list(target.iterdir())


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source_root = tmp_path/'source'
    files = {'packaging/components.lock.json':json.dumps({'frozen':{'mosdns':{'sha256':hashlib.sha256(b'frozen').hexdigest()}}}),
        'packaging/native/requirements.lock.txt':'locked-wheels\n',
        'packaging/native/install.py':'# shared installer\n',
        'packaging/native/runtime/__init__.py':'# native\n',
        'integrated/runtime/__init__.py':'# runtime\n',
        'integrated/backend/server.py':'VERSION = "before"\n',
        'web/backend/app.py':'# shared backend\n'}
    for name,data in files.items():
        p=source_root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
    monkeypatch.setitem(trial['build'].__globals__,'ROOT',source_root)
    base=tmp_path/'base'
    values={'core/dnscrypt-proxy':b'old wrapper','libexec/dnscrypt-proxy':b'old binary','libexec/mosdns':b'frozen',
        'dist/index.html':b'UI','wheels/unused.whl':b'locked wheel','requirements.lock.txt':b'locked-wheels\n',
        'app/native/__init__.py':b'old','app/runtime/__init__.py':b'old','app/backend/server.py':b'old',
        'app/base/app.py':b'old','install.py':b'old installer','build-info.json':b'{}','source-provenance.json':b'{}'}
    for name,data in values.items():
        p=base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    manifest={'product':'LovestDNS','deployment':'native','architecture':'amd64','version':'old',
        'files':{name:sha(base/name) for name in values},'components':None}
    (base/'manifest.json').write_text(json.dumps(manifest))
    archive=tmp_path/'base.tar.gz'
    with tarfile.open(archive,'w:gz') as z:z.add(base,arcname='base')
    lock=json.loads(trial['source']['LOCK'].read_text())
    candidate=tmp_path/'combined';candidate.mkdir();(candidate/'dnscrypt-proxy').write_bytes(b'normal candidate')
    m={'candidate_only':True,'diagnostic_only':False,'release_approved':False,'variant':'combined','architecture':lock['architecture'],'version':lock['version'],
        'source':lock['source'],'toolchain':lock['toolchain'],'source_lock_sha256':sha(trial['source']['LOCK']),
        'patches_sha256':{name:lock['patches'][name] for name in lock['variants']['combined']},
        'binary_sha256':sha(candidate/'dnscrypt-proxy'),'build_environment':{'CGO_ENABLED':'0'}}
    (candidate/'source-manifest.json').write_text(json.dumps(m))
    return archive,candidate,tmp_path/'out',base


def test_trial_packages_have_matched_app_and_preserve_frozen_components(inputs,tmp_path):
    archive,candidate,output,base=inputs
    before=sha(archive)
    packages=trial['build'](archive,before,candidate,output,'trial1')
    unpacked={}
    for label,metadata in packages.items():
        assert sha(output/metadata['archive'])==metadata['sha256']
        target=tmp_path/('unpack-'+label);target.mkdir()
        with tarfile.open(output/metadata['archive']) as z:
            if callable(getattr(tarfile, 'data_filter', None)):
                z.extractall(target, filter='data')
            else:
                # This fixture was just constructed by the builder above.
                z.extractall(target)
        p=next(target.iterdir());manifest=trial['installer']['verify_package'](p);unpacked[label]=p
        assert manifest['candidate_only'] and not manifest['release_approved']
        for name in ('libexec/mosdns','libexec/dnscrypt-proxy','dist/index.html','wheels/unused.whl'):
            assert (p/name).read_bytes()==(base/name).read_bytes()
    control,combined=unpacked['control'],unpacked['combined']
    assert (control/'app/runtime/__init__.py').read_bytes()==(combined/'app/runtime/__init__.py').read_bytes()
    assert (combined/'components/dnscrypt/dnscrypt-proxy').read_bytes()==(candidate/'dnscrypt-proxy').read_bytes()
    assert 'components/dnscrypt/dnscrypt-proxy' in (combined/'core/dnscrypt-proxy').read_text()
    assert (control/'core/dnscrypt-proxy').read_bytes()==(base/'core/dnscrypt-proxy').read_bytes()
    assert json.loads((combined/'build-info.json').read_text())['components']['selected']==['dnscrypt']
    assert sha(archive)==before


@pytest.mark.parametrize('fault',['archive-sha','diagnostic','payload','base-payload','architecture','cgo'])
def test_bad_trial_input_cannot_produce_an_output(inputs,fault):
    archive,candidate,output,base=inputs
    expected=sha(archive)
    if fault=='archive-sha':expected='0'*64
    elif fault=='payload':(candidate/'dnscrypt-proxy').write_bytes(b'changed')
    elif fault in ('diagnostic','architecture','cgo'):
        p=candidate/'source-manifest.json';m=json.loads(p.read_text())
        if fault=='diagnostic':m['diagnostic_only']=True
        elif fault=='architecture':m['architecture']='linux/arm64'
        else:m['build_environment']['CGO_ENABLED']='1'
        p.write_text(json.dumps(m))
    else:
        (base/'libexec/mosdns').write_bytes(b'changed')
        with tarfile.open(archive,'w:gz') as z:z.add(base,arcname='base')
        expected=sha(archive)
    with pytest.raises(ValueError):trial['build'](archive,expected,candidate,output,'trial1')
    assert not output.exists()


@pytest.mark.parametrize('fault',['traversal','symlink','hardlink','duplicate','two-roots'])
def test_unsafe_archive_is_rejected_before_extraction(tmp_path,fault):
    archive=tmp_path/'unsafe.tar';target=tmp_path/'target';target.mkdir()
    with tarfile.open(archive,'w') as z:
        entry=tarfile.TarInfo('root/file');entry.size=1;z.addfile(entry,io.BytesIO(b'x'))
        bad=tarfile.TarInfo({'traversal':'../escape','two-roots':'other/file'}.get(fault,'root/file'))
        if fault in ('symlink','hardlink'):
            bad.name='root/link';bad.type=tarfile.SYMTYPE if fault=='symlink' else tarfile.LNKTYPE;bad.linkname='/etc/passwd'
            z.addfile(bad)
        else:bad.size=1;z.addfile(bad,io.BytesIO(b'y'))
    with pytest.raises(ValueError,match='Unsafe|Expected one'):trial['extract_base'](archive,target)
    assert not list(target.iterdir())
