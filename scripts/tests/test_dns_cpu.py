import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dns_cpu import ROLES, parse_generator_threads, parse_layout, valid_placement


def layout():
    return dict(zip(ROLES, range(6)))


def test_layout_rejects_missing_duplicate_unknown_and_unavailable_roles():
    text = ','.join(f'{role}={cpu}' for role, cpu in layout().items())
    assert parse_layout(text, range(6)) == layout()
    assert parse_layout(None, []) == {}
    for invalid in (text.replace(',generator=5', ''), text + ',generator=6',
                    text.replace('generator=5', 'other=5'), text.replace('generator=5', 'generator=4'),
                    text.replace('generator=5', 'generator=-1')):
        with pytest.raises(ValueError):
            parse_layout(invalid, range(6))
    with pytest.raises(ValueError, match='allowed'):
        parse_layout(text, range(5))
    dual = text.replace('generator=5', 'generator=5+6')
    assert parse_layout(dual, range(7)) == {**layout(), 'generator': [5, 6]}
    for invalid in (dual.replace('5+6', '5+5'), dual.replace('5+6', '4+6'),
                    dual.replace('5+6', '5+6+7'), dual.replace('controller=0', 'controller=0+7')):
        with pytest.raises(ValueError):
            parse_layout(invalid, range(8))


def test_placement_rejects_false_completion_missing_roles_and_wrong_cpu():
    expected = layout()
    evidence = {'complete': True, 'expected': expected, 'samples': 1, 'mismatches': [],
                'last': {('answer_capture' if role == 'capture' else role): [cpu] for role, cpu in expected.items()}}
    assert valid_placement(evidence, expected)
    assert not valid_placement({**evidence, 'samples': 0}, expected)
    assert not valid_placement({**evidence, 'mismatches': [{'generator': [9]}]}, expected)
    assert not valid_placement({**evidence, 'last': {**evidence['last'], 'redis': [3]}}, expected)
    assert not valid_placement({**evidence, 'last': {}}, expected)
    assert not valid_placement(None, expected)
    expected = {**expected, 'generator': [5, 6]}
    evidence = {**evidence, 'expected': expected, 'last': {**evidence['last'], 'generator': [5, 6]}}
    assert not valid_placement(evidence, expected)
    evidence.update(generator_threads_checked=2, thread_mismatches=[])
    assert valid_placement(evidence, expected)
    assert not valid_placement({**evidence, 'thread_mismatches': [{'tid': 1, 'cpus': [5]}]}, expected)


def test_generator_threads_use_both_reserved_cpus_and_reject_ambiguous_roles():
    expected = {**layout(), 'generator': [5, 6]}
    assert parse_generator_threads(None, {}) == {}
    assert parse_generator_threads('receiver=6,sender=5', expected) == {'receiver': 6, 'sender': 5}
    for invalid in ('receiver=5', 'receiver=5,sender=5', 'receiver=5,sender=7', 'receiver=5,sender=-1',
                    'receiver=5,sender=6,sender=6', 'other=5,sender=6', 'receiver=5,sender=6+7'):
        with pytest.raises(ValueError):
            parse_generator_threads(invalid, expected)
    with pytest.raises(ValueError):
        parse_generator_threads('receiver=5,sender=6', layout())


def test_separate_thread_claim_requires_prestart_audit_and_each_live_thread():
    expected = {**layout(), 'generator': [5, 6]}
    cpus = {'receiver': 5, 'sender': 6}
    evidence = {'complete': True, 'expected': expected, 'samples': 1, 'mismatches': [],
                'last': {('answer_capture' if role == 'capture' else role): ([5, 6] if role == 'generator' else [cpu])
                         for role, cpu in expected.items()},
                'generator_threads_checked': 3, 'thread_mismatches': [],
                'thread_preflight': {'method': 'dnsperf-thread-affinity-before-start-v1', 'complete': True, 'requested': cpus},
                'thread_observations': {role: {'tid': 100 + cpu, 'cpus': [cpu], 'last_cpu': cpu, 'samples': 5}
                                        for role, cpu in cpus.items()}}
    assert valid_placement(evidence, expected, cpus)
    assert not valid_placement({**evidence, 'thread_preflight': {}}, expected, cpus)
    assert not valid_placement({**evidence, 'thread_observations': {}}, expected, cpus)
    evidence['thread_observations']['receiver']['cpus'] = [5, 6]
    assert not valid_placement(evidence, expected, cpus)
    evidence['thread_observations']['receiver'].update(cpus=[5], last_cpu=6)
    assert not valid_placement(evidence, expected, cpus)


@pytest.mark.skipif(not hasattr(os, 'sched_setaffinity'), reason='Linux affinity test')
def test_separate_fixture_serves_questions_on_requested_cpu():
    from dns_lab import FixtureProcess, query
    cpu = min(os.sched_getaffinity(0))
    fixture = FixtureProcess(cpu=cpu, ttl=600, threaded_udp=False, track_questions=True)
    try:
        assert fixture.process.pid != os.getpid()
        assert fixture.affinity == [cpu] == sorted(os.sched_getaffinity(fixture.process.pid))
        for tcp, qtype in ((False, 1), (True, 28)):
            answer = query(fixture.port, 'probe.cache.test', qtype=qtype, tcp=tcp)
            assert answer['answers'][0]['value'] == ('192.0.2.10' if qtype == 1 else '2001:db8::10')
        assert fixture.requests() == 2
        assert sum(fixture.question_snapshot().values()) == 2
    finally:
        fixture.close()
    assert not fixture.process.is_alive()
