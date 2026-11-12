import runpy
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
benchmark = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))
report = runpy.run_path(str(ROOT / 'scripts/report-dns-benchmark.py'))


def test_percentiles_use_individual_latencies_and_validate_sample_count():
    text = '\n'.join([f'> NOERROR name.test A {i / 1000:.6f}' for i in range(1, 101)])
    text += '\nQueries sent: 100\nQueries completed: 100 (100%)\nQueries lost: 0\nQueries per second: 100.0\nRun time (s): 1.0\n'
    result = benchmark['parse_dnsperf'](text)
    assert result['p50_ms'] == 50
    assert result['p95_ms'] == 95
    assert result['p99_ms'] == 99
    with pytest.raises(ValueError, match='sample count'):
        benchmark['parse_dnsperf'](text.replace('Queries completed: 100', 'Queries completed: 99'))


@pytest.mark.parametrize('threaded_udp', [True, False])
def test_local_fixture_checks_addresses_ipv6_negative_and_tcp_fallback(threaded_udp):
    from dns_lab import Fixture, query
    fixture = Fixture(threaded_udp=threaded_udp)
    try:
        assert query(fixture.port, 'valid.cache.test')['answers'][0]['value'] == '192.0.2.10'
        assert query(fixture.port, 'valid.cache.test', qtype=28)['answers'][0]['value'] == '2001:db8::10'
        assert query(fixture.port, 'missing.cache.test')['rcode'] == 3
        assert query(fixture.port, 'trunc.cache.test')['truncated']
        assert query(fixture.port, 'trunc.cache.test', tcp=True)['answers']
        assert query(fixture.port, 'cname.cache.test')['answers'][0]['value'] == 'target.cache.test'
    finally:
        fixture.close()


def accepted_payload():
    payload = {'environment': {'duration': 120, 'qps': 1000, 'repeats': 5, 'cases': ['baseline', 'combined'], 'scenarios': ['hot']}, 'runs': []}
    for case in ('baseline', 'combined'):
        for repeat in range(5):
            payload['runs'].append({'case': case, 'repeat': repeat, 'scenario': 'hot', 'qps': 1000,
                'p50_ms': .2, 'p95_ms': .4, 'p99_ms': .6, 'lost': 0, 'rcodes': {'NOERROR': 120000},
                'sent': 120000, 'completed': 120000, 'latency_samples': 120000, 'upstream_requests': 0,
                'run_time_seconds': 120, 'elapsed_seconds': 120.5,
                'answer_validation': {'method': report['METHOD'], 'queries_seen': 120000, 'checked': 120000,
                                      'errors': 0, 'capture_drops': 0, 'complete': True},
                'resources': {name: {'cpu_seconds': 2, 'peak_rss_bytes': 10000000} for name in ('redis', 'unbound')}})
    return payload


def test_performance_report_rejects_partial_evidence_and_tail_regression():
    payload = accepted_payload()
    assert report['summarize'](payload)['screen_passed']
    partial = deepcopy(payload)
    partial['runs'].pop()
    assert not report['summarize'](partial)['screen_passed']
    short = deepcopy(payload)
    short['environment']['duration'] = 3
    assert not report['summarize'](short)['screen_passed']
    for run in payload['runs']:
        if run['case'] == 'combined':
            run['p99_ms'] = 1
    assert not report['summarize'](payload)['screen_passed']


@pytest.mark.parametrize('runtime,completed', [(1, 1), (120, 120), (1, 120000)])
def test_report_rejects_underload_even_when_versions_match(runtime, completed):
    payload = accepted_payload()
    for run in payload['runs']:
        run.update(run_time_seconds=runtime, elapsed_seconds=runtime + .5,
                   qps=completed / runtime, completed=completed, sent=completed,
                   rcodes={'NOERROR': completed}, latency_samples=completed)
        run['answer_validation'].update(queries_seen=completed, checked=completed)
    result = report['summarize'](payload)
    assert not result['screen_passed']
    assert not result['measurement_valid']


@pytest.mark.parametrize('field,value', [('errors', 1), ('checked', 119999), ('queries_seen', 119999),
                                        ('capture_drops', 1), ('complete', False), ('method', 'prewarm-only')])
def test_report_rejects_one_invalid_answer_run_hidden_by_other_rounds(field, value):
    payload = accepted_payload()
    payload['runs'][-1]['answer_validation'][field] = value
    assert not report['summarize'](payload)['screen_passed']


def test_report_refuses_legacy_runs_without_actual_runtime_or_answer_checks():
    payload = accepted_payload()
    for run in payload['runs']:
        run.pop('answer_validation')
        run.pop('run_time_seconds')
    result = report['summarize'](payload)
    assert not result['duration_and_repeats_met']
    assert not result['screen_passed']


def test_report_rejects_placement_claim_without_actual_role_evidence():
    payload = accepted_payload()
    payload['environment']['cpu_layout'] = dict(zip(('controller', 'fixture', 'redis', 'unbound', 'capture', 'generator'), range(6)))
    for row in payload['runs']:
        row['affinity_validation'] = {'complete': True, 'expected': payload['environment']['cpu_layout']}
    assert not report['summarize'](payload)['screen_passed']


def test_report_handles_missing_resources_and_zero_completions_without_certifying():
    payload = accepted_payload()
    payload['runs'][-1]['resources']['redis']['cpu_seconds'] = None
    payload['runs'][-1]['completed'] = 0
    result = report['summarize'](payload)
    assert not result['screen_passed']


@pytest.mark.parametrize('change', ['limit', 'missing_command', 'client_drop', 'server_drop'])
def test_report_refuses_mismatched_load_limits_and_socket_loss(change):
    payload = accepted_payload()
    payload['environment']['max_outstanding'] = 64
    for run in payload['runs']:
        run.update(generator_command=['dnsperf', '-q', '64'], client_udp={'drops': 0}, server_udp={'drops': 0})
    assert report['summarize'](payload)['screen_passed']
    run = payload['runs'][-1]
    if change == 'limit':
        run['generator_command'][-1] = '256'
    elif change == 'missing_command':
        run.pop('generator_command')
    else:
        run['client_udp' if change == 'client_drop' else 'server_udp']['drops'] = 1
    assert not report['summarize'](payload)['screen_passed']


def cachedb_payload():
    payload = accepted_payload()
    payload['environment']['scenarios'] = ['cachedb']
    payload['environment']['entry'] = 'unbound'
    for run in payload['runs']:
        run['scenario'] = 'cachedb'
        run['input_sha256'] = 'a' * 64
        entries = run['sent'] + 10
        run['cachedb_validation'] = {
            'seed_queries': entries, 'redis_entries': entries,
            'seed_unbound_pid': 1, 'measured_unbound_pid': 2,
            'seed_redis_pid': 3, 'measured_redis_pid': 3,
            'redis_delta': {'keyspace_hits': run['completed'], 'keyspace_misses': 0},
            'seed': {'sent': entries, 'completed': entries, 'lost': 0,
                     'upstream_requests': entries, 'redis_entries': entries,
                     'input_sha256': run['input_sha256'], 'rcodes': {'NOERROR': entries},
                     'client_udp': {'drops': 0}, 'server_udp': {'drops': 0},
                     'answer_validation': {**run['answer_validation'], 'checked': entries, 'queries_seen': entries}}}
    return payload


@pytest.mark.parametrize('change', ['warm_memory', 'redis_restarted', 'upstream', 'hits', 'misses',
                                    'incomplete_seed', 'different_input', 'missing_evidence'])
def test_cachedb_requires_real_redis_hits_after_clearing_only_unbound_memory(change):
    payload = cachedb_payload()
    assert report['summarize'](payload)['screen_passed']
    run = payload['runs'][-1]
    cachedb = run['cachedb_validation']
    if change == 'warm_memory':
        cachedb['measured_unbound_pid'] = cachedb['seed_unbound_pid']
    elif change == 'redis_restarted':
        cachedb['measured_redis_pid'] += 1
    elif change == 'upstream':
        run['upstream_requests'] = 1
    elif change == 'hits':
        cachedb['redis_delta']['keyspace_hits'] -= 1
    elif change == 'misses':
        cachedb['redis_delta']['keyspace_misses'] = 1
    elif change == 'incomplete_seed':
        cachedb['seed']['answer_validation']['complete'] = False
    elif change == 'different_input':
        cachedb['seed']['input_sha256'] = 'b' * 64
    else:
        run.pop('cachedb_validation')
    result = report['summarize'](payload)
    assert not result['screen_passed']
    assert 'cache_path' in result['rows'][-1]['invalid_runs'][0]['failed_checks']


@pytest.mark.parametrize('problem', [None, 'tls', 'route', 'cache', 'missing', 'socks'])
def test_dnscrypt_report_requires_tls_real_route_and_cold_upstream_work(problem):
    payload = accepted_payload();payload['environment']['entry'] = 'dnscrypt'
    payload['environment']['scenarios'] = ['doh-socks']
    for run in payload['runs']:
        n = run['completed'];run.update(scenario='doh-socks', tls_verified=True, upstream_requests=n,
            route_validation={'doh_routes': {'direct': 0, 'proxy': n + 1, 'rejected': 0}, 'socks_connections': 1})
        run['resources'] = {'dnscrypt': {'cpu_seconds': 2, 'peak_rss_bytes': 10000000}}
    run = payload['runs'][-1]
    if problem == 'tls': run['tls_verified'] = False
    elif problem == 'route': run['route_validation']['doh_routes']['direct'] = 1
    elif problem == 'cache': run['upstream_requests'] = 0
    elif problem == 'missing': run.pop('route_validation')
    elif problem == 'socks': run['route_validation']['socks_connections'] = 0
    assert report['summarize'](payload)['screen_passed'] is (problem is None)
