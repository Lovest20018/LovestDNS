from collections import Counter
from copy import deepcopy
from pathlib import Path
import runpy
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dns_lab import Fixture, question
from dns_seed import PROTOCOL, coverage, valid_seed_path


def seed_evidence():
    expected = {(f'name-{i}.cache.test', 1, 1) for i in range(200)}
    actual = Counter({(*key, 'udp'): 1 for key in expected})
    actual[('name-0.cache.test', 1, 1, 'udp')] += 1
    row = coverage(expected, Counter(), actual, 'a' * 64)
    return {'input_sha256': 'a' * 64, 'upstream_requests': 201, 'question_coverage': row}


def test_fixture_tracks_case_insensitive_type_class_and_transport_without_requiring_a_server():
    fixture = Fixture.__new__(Fixture)
    import threading
    fixture.lock = threading.Lock(); fixture.counts = Counter(); fixture.question_counts = Counter()
    fixture.delay = 0; fixture.address = '192.0.2.10'; fixture.ttl = 600
    fixture.respond(question('MiXeD.cache.test'), False)
    fixture.respond(question('mixed.cache.test'), True)
    fixture.respond(question('mixed.cache.test', 28), False)
    assert fixture.requests() == 3
    assert fixture.question_snapshot() == {('mixed.cache.test', 1, 1, 'udp'): 1,
                                           ('mixed.cache.test', 1, 1, 'tcp'): 1,
                                           ('mixed.cache.test', 28, 1, 'udp'): 1}


def test_proven_repeated_attempt_does_not_mean_an_incomplete_seed():
    seed = seed_evidence()
    assert valid_seed_path(seed, 200, PROTOCOL)
    assert not valid_seed_path(seed, 200)  # Historical strict protocol stays strict.
    assert seed['question_coverage']['additional_attempts'] == 1


@pytest.mark.parametrize('change', ['missing', 'unexpected', 'missing_ledger', 'different_input',
                                    'unexplained_count', 'wrong_type', 'bad_transport', 'excessive_attempts', 'unknown_protocol'])
def test_missing_or_unexplained_seed_evidence_is_rejected(change):
    seed = seed_evidence(); protocol = PROTOCOL
    row = seed['question_coverage']
    if change == 'missing': row['missing_unique'] = 1
    elif change == 'unexpected': row['unexpected_unique'] = 1
    elif change == 'missing_ledger': seed.pop('question_coverage')
    elif change == 'different_input': row['input_sha256'] = 'b' * 64
    elif change == 'unexplained_count': seed['upstream_requests'] += 1
    elif change == 'wrong_type': row['additional_attempts'] = True
    elif change == 'bad_transport': row['transport_attempts']['udp'] -= 1
    elif change == 'excessive_attempts':
        row.update(attempts=203, additional_attempts=3, transport_attempts={'udp': 203, 'tcp': 0})
        seed['upstream_requests'] = 203
    elif change == 'unknown_protocol': protocol = 'unreviewed'
    assert not valid_seed_path(seed, 200, protocol)


def test_coverage_detects_a_missing_question_even_when_total_attempts_match():
    expected = {('a.cache.test', 1, 1), ('b.cache.test', 1, 1)}
    actual = Counter({('a.cache.test', 1, 1, 'udp'): 2})
    row = coverage(expected, Counter(), actual, 'a' * 64)
    assert not row['complete'] and row['missing_unique'] == 1


def test_report_v2_keeps_measured_redis_path_strict():
    tests = runpy.run_path(str(Path(__file__).with_name('test_dns_benchmark.py')))
    payload = tests['cachedb_payload']()
    payload['environment']['cachedb_seed_protocol'] = PROTOCOL
    for run in payload['runs']:
        seed = run['cachedb_validation']['seed']; entries = seed['sent']
        seed['upstream_requests'] = entries + 1
        seed['question_coverage'] = {'method': 'fixture-question-ledger-v1', 'input_sha256': seed['input_sha256'],
            'expected_unique': entries, 'observed_unique': entries, 'missing_unique': 0, 'unexpected_unique': 0,
            'attempts': entries + 1, 'additional_attempts': 1, 'transport_attempts': {'udp': entries + 1, 'tcp': 0},
            'ledger_sha256': 'c' * 64, 'complete': True}
    report = tests['report']
    assert report['summarize'](payload)['screen_passed']
    for field in ['upstream_requests', 'keyspace_misses']:
        broken = deepcopy(payload)
        if field == 'upstream_requests': broken['runs'][-1][field] = 1
        else: broken['runs'][-1]['cachedb_validation']['redis_delta'][field] = 1
        assert not report['summarize'](broken)['screen_passed']
