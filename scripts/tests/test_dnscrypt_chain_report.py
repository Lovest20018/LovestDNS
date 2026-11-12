from copy import deepcopy
from pathlib import Path
import runpy

import pytest

module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'report-dnscrypt-chain.py'))
summary = module['summarize']


def evidence(duration=30, repeats=3):
    env = {'duration': duration, 'repeats': repeats, 'routes': ['direct'], 'scenarios': ['split-cold'],
           'cases': ['baseline', 'upstream', 'cache']}
    runs = []
    for case in env['cases']:
        for repeat in range(repeats):
            runs.append({'case': case, 'repeat': repeat, 'scenario': 'split-cold', 'route': 'direct', 'completed': 3000,
                         'measurement_checks': {key: True for key in module['REQUIRED_CHECKS']}, 'p95_ms': 1, 'p99_ms': 2, 'qps': 100,
                         'resources': {name: {'cpu_seconds': 1, 'peak_rss_bytes': 10000}
                                       for name in ('dnscrypt', 'mosdns', 'unbound', 'redis')}})
    return {'environment': env, 'runs': runs}


def test_prescreen_does_not_approve_release_or_formal_performance():
    result = summary(evidence())
    assert result['prescreen_passed']
    assert not result['configured_matrix_performance_passed'] and not result['release_approved']
    assert summary(evidence(120, 5))['configured_matrix_performance_passed']
    assert not summary(evidence(5, 1))['prescreen_passed']


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'unexpected', 'pacing', 'missing_check', 'nan_resource'])
def test_incomplete_or_invalid_round_cannot_pass(change):
    data = evidence()
    if change == 'missing': data['runs'].pop()
    elif change == 'duplicate': data['runs'][-1]['repeat'] = 0
    elif change == 'unexpected': data['runs'].append({**deepcopy(data['runs'][-1]), 'route': 'socks'})
    elif change == 'pacing': data['runs'][-1]['measurement_checks']['pacing'] = False
    elif change == 'missing_check': data['runs'][-1]['measurement_checks'].pop('split_path')
    else: data['runs'][-1]['resources']['dnscrypt']['cpu_seconds'] = float('nan')
    assert not summary(data)['prescreen_passed']


def test_aggregate_cpu_cannot_hide_dnscrypt_regression():
    data = evidence()
    for run in data['runs']:
        if run['case'] == 'cache':
            run['resources']['dnscrypt']['cpu_seconds'] = 1.1
            run['resources']['mosdns']['cpu_seconds'] = .9
    result = summary(data)
    compared = next(c for c in result['comparisons'] if c['case'] == 'cache' and c['reference'] == 'upstream')
    assert compared['gates']['total_cpu'] and not compared['gates']['dnscrypt_cpu']
    assert not result['prescreen_passed']


def test_tail_latency_limit_is_preserved():
    data = evidence()
    for run in data['runs']:
        if run['case'] == 'cache': run['p99_ms'] = 2.3
    assert not summary(data)['prescreen_passed']


def test_combined_candidate_is_compared_to_both_references():
    data = evidence()
    data['environment']['cases'][-1] = 'combined'
    for run in data['runs']:
        if run['case'] == 'cache':
            run['case'] = 'combined'
            run['resources']['dnscrypt']['cpu_seconds'] = 1.1
    result = summary(data)
    comparisons = [row for row in result['comparisons'] if row['case'] == 'combined']
    assert {row['reference'] for row in comparisons} == {'baseline', 'upstream'}
    assert not any(row['gates']['dnscrypt_cpu'] for row in comparisons)
    assert not result['prescreen_passed']
