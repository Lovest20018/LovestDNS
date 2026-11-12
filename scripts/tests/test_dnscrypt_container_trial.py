"""Reject unreviewed candidates and changed frozen payloads before building images."""
import hashlib
from copy import deepcopy
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
trial = runpy.run_path(str(ROOT/'scripts/build-dnscrypt-container-trial.py'))
smoke = runpy.run_path(str(ROOT/'scripts/test-integrated-container.py'))
BASE = 'sha256:'+'1'*64


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    candidate = tmp_path/'combined'; candidate.mkdir()
    (candidate/'dnscrypt-proxy').write_bytes(b'ordinary-static-binary')
    lock = json.loads(trial['source']['LOCK'].read_text())
    m = {'candidate_only':True,'diagnostic_only':False,'release_approved':False,'variant':'combined',
         'architecture':lock['architecture'],'version':lock['version'],'source':lock['source'],'toolchain':lock['toolchain'],
         'source_lock_sha256':trial['digest'](trial['source']['LOCK']),
         'patches_sha256':{p:lock['patches'][p] for p in lock['variants']['combined']},
         'binary_sha256':hashlib.sha256((candidate/'dnscrypt-proxy').read_bytes()).hexdigest(),
         'build_environment':{'CGO_ENABLED':'0'}}
    (candidate/'source-manifest.json').write_text(json.dumps(m))
    frozen = json.loads((ROOT/'packaging/components.lock.json').read_text())['frozen']['mosdns']['sha256']
    before = {p:frozen if p.endswith('/mosdns') else 'old' for p in (*trial['FROZEN'],'/usr/bin/dnscrypt-proxy')}
    ids = {'lovestdns:dnstrial-test-control':'sha256:'+'2'*64,'lovestdns:dnstrial-test-combined':'sha256:'+'3'*64}
    def docker(*args):
        if args[:2] == ('image','inspect'):
            return json.dumps([{'Id':ids.get(args[2],BASE),'RepoDigests':[]}])
        return '{}'
    def image_files(image):
        return {**before,'/usr/bin/dnscrypt-proxy':m['binary_sha256']} if image == ids['lovestdns:dnstrial-test-combined'] else dict(before)
    g = trial['build'].__globals__
    monkeypatch.setitem(g,'docker',docker); monkeypatch.setitem(g,'image_files',image_files)
    monkeypatch.setattr(g['subprocess'],'run',lambda *a,**k:SimpleNamespace(returncode=1 if a[0][1:3]==['image','inspect'] else 0))
    return candidate,tmp_path/'output',m,before


def test_matched_image_context_keeps_shared_app_and_marks_actual_candidate(prepared):
    candidate,out,m,_ = prepared
    result = trial['build'](BASE,candidate,out,'lovestdns:dnstrial-test')
    assert result['build_complete'] and not result['release_approved']
    control = json.loads((out/'context/control-info.json').read_text())
    combined = json.loads((out/'context/combined-info.json').read_text())
    assert control['trial']['inputs_sha256'] == combined['trial']['inputs_sha256']
    assert combined['components']['versions']['dnscrypt'] == m['version']
    assert (out/'context/dnscrypt-proxy').read_bytes() == (candidate/'dnscrypt-proxy').read_bytes()


@pytest.mark.parametrize('fault',['diagnostic','payload','architecture','cgo','mosdns'])
def test_invalid_inputs_are_rejected_before_output(prepared,fault):
    candidate,out,m,before = prepared
    if fault == 'payload': (candidate/'dnscrypt-proxy').write_bytes(b'changed')
    elif fault == 'mosdns': before['/usr/sbin/mosdns'] = 'changed'
    else:
        if fault == 'diagnostic': m['diagnostic_only'] = True
        elif fault == 'architecture': m['architecture'] = 'linux/arm64'
        else: m['build_environment']['CGO_ENABLED'] = '1'
        (candidate/'source-manifest.json').write_text(json.dumps(m))
    with pytest.raises(ValueError): trial['build'](BASE,candidate,out,'lovestdns:dnstrial-test')
    assert not out.exists()


def protocol_counts():
    before = {'requests':10,'rejected':0,'dns_errors':3,'routes':{'direct':11,'proxy':2},
              'protocols':{'HTTP/2.0':13},'alpn':{'h2':13}}
    after = deepcopy(before)
    after['requests'] += 4
    after['routes']['direct'] += 2; after['routes']['proxy'] += 2
    after['protocols']['HTTP/2.0'] += 4; after['alpn']['h2'] += 4
    return before,after


def test_protocol_interval_keeps_previous_fixture_errors_without_charging_them_again():
    before,after = protocol_counts()
    evidence = smoke['encrypted_evidence'](before,after)
    assert evidence['valid'] and evidence['before']['dns_errors']==3
    assert evidence['deltas']['dns_errors']==0


@pytest.mark.parametrize('fault',['dns_errors','rejected','requests','route','alpn'])
def test_new_protocol_errors_wrong_counts_and_routes_remain_rejected(fault):
    before,after = protocol_counts()
    if fault in ('dns_errors','rejected','requests'): after[fault] += 1
    elif fault == 'route': after['routes']['proxy'] -= 1
    else: after['alpn'] = {'h2':13,'http/1.1':4}
    assert not smoke['encrypted_evidence'](before,after)['valid']
