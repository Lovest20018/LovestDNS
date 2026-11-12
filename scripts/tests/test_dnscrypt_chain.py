from collections import Counter
from copy import deepcopy
from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
chain = runpy.run_path(str(SCRIPTS / 'benchmark-dnscrypt-chain.py'))


def observed(scenario='split-cold'):
    rows = chain['question_rows'](scenario, 100)
    local, encrypted = chain['expected_queries'](scenario, rows)
    before = {'requests': 0, 'connections': 0, 'rejected': 0, 'dns_errors': 0,
              'routes': {}, 'protocols': {}, 'alpn': {}}
    after = {**deepcopy(before), 'requests': encrypted}
    if encrypted:
        after.update(routes={'direct': encrypted}, protocols={'HTTP/2.0': encrypted}, alpn={'h2': encrypted})
    counters = {'keyspace_hits': 0, 'keyspace_misses': 0}
    actual = Counter({(*key, 'udp'): count for key, count in local.items()})
    return rows, Counter(), actual, before, after, counters, dict(counters), 'h2'


def test_workloads_have_declared_unique_or_bounded_hot_keys():
    for scenario in chain['SCENARIOS']:
        rows = chain['question_rows'](scenario, 100)
        assert len(rows) == 100 and sum(kind == 'AAAA' for _, kind in rows) == 20
        assert len(set(rows)) == (100 if scenario.endswith('-cold') else 32)
        local, encrypted = chain['expected_queries'](scenario, rows)
        assert sum(local.values()) == (50 if scenario == 'split-cold' else 0)
        assert encrypted == (100 if scenario == 'dnscrypt-cold' else 50 if scenario == 'split-cold' else 0)


@pytest.mark.parametrize('scenario', chain['SCENARIOS'])
def test_exact_observed_paths_are_accepted(scenario):
    evidence = chain['path_evidence'](scenario, *observed(scenario))
    assert evidence['valid']


@pytest.mark.parametrize('change', ['name', 'type', 'count', 'alpn'])
def test_cold_split_rejects_wrong_names_types_counts_or_http(change):
    args = list(observed())
    if change in ('name', 'type'):
        key = next(iter(args[2])); count = args[2].pop(key)
        replacement = ('wrong.local.cache.test', *key[1:]) if change == 'name' else (key[0], 65, *key[2:])
        args[2][replacement] = count
    elif change == 'count':
        args[4]['requests'] += 1
    else:
        args[4]['alpn'] = {'http/1.1': 50}
    assert not chain['path_evidence']('split-cold', *args)['valid']


@pytest.mark.parametrize('backend', ['redis', 'local', 'encrypted'])
def test_hot_topology_cannot_be_certified_with_hidden_backend_access(backend):
    args = list(observed('split-hot'))
    if backend == 'redis': args[6]['keyspace_hits'] = 1
    elif backend == 'local': args[2][('unexpected.local.cache.test', 1, 1, 'udp')] = 1
    else: args[4]['requests'] = 1
    assert not chain['path_evidence']('split-hot', *args)['valid']
