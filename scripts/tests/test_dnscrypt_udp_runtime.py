from copy import deepcopy
from pathlib import Path
import runpy
import struct
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
module = runpy.run_path(str(SCRIPTS/'test-dnscrypt-udp-runtime.py'))
from dns_lab import Fixture, read_name


@pytest.mark.parametrize('size',[256,4095])
def test_padded_queries_have_exact_size_and_valid_opt(size):
    raw = module['packet']('queue.cache.test',28,123,size)
    _, end = read_name(raw,12)
    assert len(raw)==size and raw[10:12]==b'\0\1'
    root, kind, payload, flags, length = struct.unpack('!BHHIH',raw[end+4:end+15])
    assert (root,kind,payload,flags)==(0,41,4096,0)
    assert length==size-end-15 and struct.unpack('!H',raw[end+15:end+17])[0]==12


def test_exact_answers_reject_duplicates_wrong_types_and_wrong_names():
    fixture = Fixture.__new__(Fixture)
    from collections import Counter
    import threading
    fixture.address,fixture.ttl='192.0.2.10',600
    fixture.counts,fixture.question_counts,fixture.lock,fixture.delay=Counter(),None,threading.Lock(),0
    response=fixture.respond(module['packet']('queue.cache.test',1,123),False)
    expected={123:('queue.cache.test',1)}
    module['check_answer'](response,expected)
    assert not expected
    for expected in ({}, {123:('wrong.cache.test',1)}, {123:('queue.cache.test',28)}):
        with pytest.raises(ValueError): module['check_answer'](response,deepcopy(expected))


def sockets(buffer=425984,drops=0,rx=0):
    return {'receive_buffer':buffer,'drops':drops,'rx_bytes':rx}


def test_known_small_queue_is_a_counterexample_and_not_a_lossless_pass():
    checks=module['queue_checks']('upstream',True,{'rmem_max':212992,'rmem_default':212992},
        sockets(8192),sockets(8192,119,8640),sockets(8192,119),9)
    assert all(checks.values())
    checks=module['queue_checks']('buffer',True,{'rmem_max':212992,'rmem_default':212992},
        sockets(),sockets(drops=119,rx=8640),sockets(drops=119),9)
    assert not checks['burst']


@pytest.mark.parametrize('admin',[False,True])
def test_combined_candidate_cannot_pass_by_reproducing_the_small_queue(admin):
    kernel={'rmem_max':212992,'rmem_default':212992}
    assert all(module['queue_checks']('combined',admin,kernel,sockets(),sockets(rx=122880),sockets(),128).values())
    assert not all(module['queue_checks']('combined',admin,kernel,sockets(8192),
        sockets(8192,119,8640),sockets(8192,119),9).values())


@pytest.mark.parametrize('change',['unexplained_loss','extra_drop','undrained','wrong_buffer'])
def test_clamping_and_loss_accounting_cannot_be_skipped(change):
    before,queued,after=sockets(),sockets(rx=122880),sockets()
    received=128
    if change=='unexplained_loss': received=127
    elif change=='extra_drop': after['drops']=1
    elif change=='undrained': after['rx_bytes']=960
    else: before['receive_buffer']=524288
    assert not all(module['queue_checks']('udp',False,{'rmem_max':212992,'rmem_default':212992},before,queued,after,received).values())


def kernel_view(tmp_path,monkeypatch,*,same_namespace=False):
    root=tmp_path/'core';root.mkdir()
    current=tmp_path/'current';current.touch()
    reference=current if same_namespace else tmp_path/'reference'
    reference.touch()
    real_path=Path
    def paths(*parts):
        path=real_path(*parts)
        if path==real_path('/proc/self/ns/net'):return current
        if path==real_path('/proc/1/ns/net'):return reference
        if path.parent==real_path('/proc/sys/net/core'):return root/path.name
        return path
    monkeypatch.setitem(module['limit_evidence'].__globals__,'Path',paths)
    return root


def test_visible_limits_are_read_without_entering_another_namespace(tmp_path,monkeypatch):
    root=kernel_view(tmp_path,monkeypatch)
    for name in ('rmem_max','rmem_default'):(root/name).write_text('212992\n')
    def forbidden(*args,**kwargs):raise AssertionError('No fallback for a visible procfs view')
    monkeypatch.setattr(module['subprocess'],'check_output',forbidden)
    row=module['limit_evidence']()
    assert row['values']=={'rmem_max':212992,'rmem_default':212992}
    assert row['source']=='current-namespace-procfs' and not row['host_sysctl_writes']


def test_missing_limits_use_only_a_readonly_reference_command(tmp_path,monkeypatch):
    root=kernel_view(tmp_path,monkeypatch);calls=[]
    def read(args,**kwargs):
        calls.append((args,kwargs));return '212992\n212992\n'
    monkeypatch.setattr(module['subprocess'],'check_output',read)
    row=module['limit_evidence']()
    assert row['source']=='reference-namespace-readonly-cat'
    assert row['namespace_inode']!=row['reference_namespace_inode']
    assert calls==[(['nsenter','--net=/proc/1/ns/net','--','cat',str(root/'rmem_max'),str(root/'rmem_default')],
                   {'text':True,'timeout':5})]


@pytest.mark.parametrize('fault',['partial-view','reference-namespace'])
def test_ambiguous_kernel_views_do_not_fall_back(tmp_path,monkeypatch,fault):
    root=kernel_view(tmp_path,monkeypatch,same_namespace=fault=='reference-namespace')
    if fault=='partial-view':(root/'rmem_max').write_text('212992\n')
    def forbidden(*args,**kwargs):raise AssertionError('Unsafe fallback')
    monkeypatch.setattr(module['subprocess'],'check_output',forbidden)
    with pytest.raises(ValueError,match='Incomplete|reference namespace'):module['limit_evidence']()


@pytest.mark.parametrize('output',['','212992\n','212992\n212992\n1\n','-1\n212992\n','x\n212992\n','0\n212992\n','2147483648\n212992\n'])
def test_invalid_reference_limits_cannot_certify_queue_capacity(tmp_path,monkeypatch,output):
    kernel_view(tmp_path,monkeypatch)
    monkeypatch.setattr(module['subprocess'],'check_output',lambda *a,**kw:output)
    with pytest.raises(ValueError,match='Invalid kernel'):module['limit_evidence']()


@pytest.mark.parametrize('admin',[False,True])
def test_low_receive_limit_cannot_turn_candidate_packet_loss_into_a_pass(admin):
    checks=module['queue_checks']('combined',admin,{'rmem_max':4096,'rmem_default':212992},
        sockets(8192),sockets(8192,119,8640),sockets(8192,119),9)
    assert checks['buffer'] and checks['drop_accounting'] and not checks['burst']
