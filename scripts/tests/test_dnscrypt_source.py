"""Reject unbound artifacts and invalid pacing before trusting candidate results."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
builder = runpy.run_path(str(SCRIPTS / 'build-dnscrypt-source.py'))
bench = runpy.run_path(str(SCRIPTS / 'benchmark-dnscrypt-source.py'))


@pytest.mark.parametrize('which', ['source', 'toolchain', 'patch'])
def test_corrupt_locked_input_never_creates_build_directory(tmp_path, monkeypatch, which):
    source, toolchain, patch = (tmp_path / name for name in ('source', 'toolchain', 'fix.patch'))
    for path in (source, toolchain, patch):
        path.write_bytes(b'verified input')
    good = hashlib.sha256(b'verified input').hexdigest()
    lock = tmp_path / 'lock.json'
    lock.write_text(json.dumps({'source': {'sha256': good}, 'toolchain': {'sha256': good},
                                'variants': {'udp': ['fix.patch']}, 'patches': {'fix.patch': good}}))
    scope = builder['verified_inputs'].__globals__
    monkeypatch.setitem(scope, 'LOCK', lock)
    monkeypatch.setitem(scope, 'PATCHES', tmp_path)
    {'source': source, 'toolchain': toolchain, 'patch': patch}[which].write_bytes(b'changed')
    with pytest.raises(ValueError, match='digest mismatch'):
        builder['build'](source, toolchain, tmp_path / 'build', ['udp'])
    assert not (tmp_path / 'build').exists()


def candidate(tmp_path):
    lock = json.loads(bench['LOCK'].read_text())
    binary = tmp_path / 'dnscrypt-proxy'
    binary.write_bytes(b'normal candidate')
    manifest = {'candidate_only': True, 'diagnostic_only': False, 'release_approved': False,
                'variant': 'udp', 'source_lock_sha256': bench['digest'](bench['LOCK']),
                'patches_sha256': {name: lock['patches'][name] for name in lock['variants']['udp']},
                'source': lock['source'], 'toolchain': lock['toolchain'], 'binary_sha256': bench['digest'](binary)}
    return binary, manifest


@pytest.mark.parametrize('change', ['diagnostic', 'patch', 'binary', 'lock'])
def test_candidate_identity_cannot_be_replaced(tmp_path, change):
    binary, manifest = candidate(tmp_path)
    if change == 'diagnostic':
        manifest['diagnostic_only'] = True
    elif change == 'patch':
        manifest['patches_sha256'] = {}
    elif change == 'binary':
        binary.write_bytes(b'replaced')
    else:
        manifest['source_lock_sha256'] = '0' * 64
    (tmp_path / 'source-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        bench['source_binary'](tmp_path)


def test_valid_pacing_and_artifact_are_accepted(tmp_path):
    binary, manifest = candidate(tmp_path)
    (tmp_path / 'source-manifest.json').write_text(json.dumps(manifest))
    assert bench['source_binary'](tmp_path)[0] == binary.resolve()
    assert bench['pacing_evidence'](paced(), 100)['valid']


def test_patched_candidate_cannot_be_mislabeled_as_upstream(tmp_path):
    _, manifest = candidate(tmp_path)
    (tmp_path / 'source-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='wrong comparison case'):
        bench['source_binary'](tmp_path, 'upstream')


def test_combined_candidate_requires_all_three_patches(tmp_path):
    binary, manifest = candidate(tmp_path)
    lock = json.loads(bench['LOCK'].read_text())
    manifest['variant'] = 'combined'
    manifest['patches_sha256'] = {name: lock['patches'][name] for name in lock['variants']['combined']}
    path = tmp_path / 'source-manifest.json'
    path.write_text(json.dumps(manifest))
    assert bench['source_binary'](tmp_path, 'combined')[0] == binary.resolve()
    for patch in tuple(manifest['patches_sha256']):
        partial = copy.deepcopy(manifest)
        partial['patches_sha256'].pop(patch)
        path.write_text(json.dumps(partial))
        with pytest.raises(ValueError, match='source lock'):
            bench['source_binary'](tmp_path, 'combined')


def test_baseline_identity_covers_the_real_payload_and_private_loader(tmp_path):
    for directory in ('core', 'libexec', 'lib'):
        (tmp_path / directory).mkdir()
    (tmp_path / 'core/dnscrypt-proxy').write_bytes(b'wrapper')
    payload = tmp_path / 'libexec/dnscrypt-proxy'
    loader = tmp_path / 'lib/ld-musl.so'
    payload.write_bytes(b'old binary')
    loader.write_bytes(b'private loader')
    _, original = bench['baseline_binary'](tmp_path)
    payload.write_bytes(b'replaced binary')
    _, changed = bench['baseline_binary'](tmp_path)
    assert changed['binary_sha256'] != original['binary_sha256']
    assert changed['runtime_sha256']['core/dnscrypt-proxy'] == original['runtime_sha256']['core/dnscrypt-proxy']
    loader.write_bytes(b'replaced loader')
    _, final = bench['baseline_binary'](tmp_path)
    assert final['runtime_sha256']['lib/ld-musl.so'] != changed['runtime_sha256']['lib/ld-musl.so']


def paced():
    return {'sent': 100, 'answer_validation': {'arrival_timing': {
        'method': 'linux-kernel-so-timestampns-v1', 'streams': {'query': {
            'count': 100, 'out_of_order': 0, 'min_gap_ns': 9_000_000,
            'max_gap_ns': 11_000_000, 'peak_packets_per_1ms': 1}}}}}


@pytest.mark.parametrize('change', [{'min_gap_ns': 1}, {'max_gap_ns': 600_000_000},
                                   {'peak_packets_per_1ms': 2}, {'count': 99}, {'out_of_order': 1}])
def test_mean_qps_cannot_hide_catchup_gaps_or_missing_packets(change):
    row = paced()
    row['answer_validation']['arrival_timing']['streams']['query'].update(change)
    assert not bench['pacing_evidence'](row, 100)['valid']


def test_http2_requires_observed_http2_and_alpn_not_a_configuration_label():
    before = {'routes': {}, 'protocols': {}, 'alpn': {}, 'requests': 0, 'connections': 0,
              'rejected': 0, 'dns_errors': 0}
    after = {**copy.deepcopy(before), 'routes': {'direct': 10}, 'requests': 10,
             'connections': 1, 'protocols': {'HTTP/2.0': 10}, 'alpn': {'h2': 10}}
    assert bench['protocol_evidence'](before, after, 'h2')['valid']
    after['alpn'] = {'http/1.1': 10}
    assert not bench['protocol_evidence'](before, after, 'h2')['valid']
