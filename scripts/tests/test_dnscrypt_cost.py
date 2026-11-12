"""The offline cost harness must bind upstream inputs and extracted functions."""
import hashlib
import json
from pathlib import Path
import runpy

import pytest

RUNNER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'benchmark-dnscrypt-cost.py'))


@pytest.mark.parametrize('corrupt', ['source', 'toolchain'])
def test_bad_cost_input_is_rejected_before_work_creation(tmp_path, monkeypatch, corrupt):
    source, toolchain = tmp_path / 'source', tmp_path / 'go'
    for path in (source, toolchain):
        path.write_bytes(b'verified')
    good = hashlib.sha256(b'verified').hexdigest()
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({'cases': {'old-newgo':['2.1.5','go1.27.3']},
                                'sources':{'2.1.5':good}, 'toolchains':{'go1.27.3':good}}))
    monkeypatch.setitem(RUNNER['prepare'].__globals__, 'LOCK', lock)
    {'source':source, 'toolchain':toolchain}[corrupt].write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='digest mismatch'):
        RUNNER['prepare'](source, toolchain, tmp_path / 'work', 'old-newgo')
    assert not (tmp_path / 'work').exists()


def test_extraction_preserves_the_selected_upstream_body():
    body = 'func chosen(v int) int {\n\tif v > 1 {\n\t\treturn v\n\t}\n\treturn 0\n}\n'
    source = 'package main\n// upstream comment\n' + body + '\nfunc other() {\n}\n'
    assert RUNNER['function'](source, 'chosen') == body
    for bad in ('package main', source + body):
        with pytest.raises(ValueError, match='exactly one'):
            RUNNER['function'](bad, 'chosen')


def test_changed_cost_patch_is_rejected_before_work_creation(tmp_path, monkeypatch):
    source, toolchain, patch = (tmp_path / name for name in ('source', 'go', 'fix.patch'))
    for path in (source, toolchain, patch):
        path.write_bytes(b'verified')
    good = hashlib.sha256(b'verified').hexdigest()
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({'cases': {'patched':['2.1.20','go1.27.3']},
        'sources':{'2.1.20':good}, 'toolchains':{'go1.27.3':good},
        'patches':{'fix.patch':good}, 'case_patches':{'patched':['fix.patch']}}))
    monkeypatch.setitem(RUNNER['prepare'].__globals__, 'LOCK', lock)
    monkeypatch.setitem(RUNNER['prepare'].__globals__, 'PATCHES', tmp_path)
    patch.write_bytes(b'changed')
    with pytest.raises(ValueError, match='digest mismatch'):
        RUNNER['prepare'](source, toolchain, tmp_path / 'work', 'patched')
    assert not (tmp_path / 'work').exists()


def test_new_cache_matrix_cannot_silently_measure_the_old_implementation(tmp_path):
    with pytest.raises(ValueError, match='requires the locked 2.1.20'):
        RUNNER['prepare'](tmp_path/'source', tmp_path/'go', tmp_path/'work', 'old-newgo', cache_matrix=True)
    assert not (tmp_path/'work').exists()


@pytest.mark.parametrize('version', ['2.1.5','2.1.20'])
def test_adapters_cannot_leave_unexpanded_behavior_placeholders(version):
    text, library = RUNNER['adapter'](version)
    assert '"' + library + '"' in text
    for marker in ('UNPACK','PACK','CLONE','TTL','RESET','CD_ISOLATED'):
        assert ('{ ' + marker + ' }') not in text
    assert 'func costChangeTTL(msg *dns.Msg, ttl uint32)' in text
