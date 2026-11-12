"""Summarize controlled split-chain evidence without approving a component release."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

COMPONENTS = ('dnscrypt', 'mosdns', 'unbound', 'redis')
REQUIRED_CHECKS = {'load_configuration', 'encrypted_path', 'cache_path', 'duration', 'offered_load',
                   'effective_qps', 'counts', 'latencies', 'resources', 'answers', 'all_components',
                   'split_path', 'pacing', 'wire_timing', 'sender'}


def valid_run(run):
    checks = run.get('measurement_checks', {})
    if not REQUIRED_CHECKS <= checks.keys() or not all(value is True for value in checks.values()):
        return False
    count = run.get('completed')
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return False
    values = [run.get(name) for name in ('p95_ms', 'p99_ms', 'qps')]
    values += [run.get('resources', {}).get(name, {}).get(field) for name in COMPONENTS
               for field in ('cpu_seconds', 'peak_rss_bytes')]
    return all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in values)


def summarize(payload):
    env, runs = payload['environment'], payload['runs']
    groups = defaultdict(list)
    allowed = {(route, scenario, case) for route in env['routes'] for scenario in env['scenarios'] for case in env['cases']}
    unexpected = []
    for run in runs:
        key = (run['route'], run['scenario'], run['case'])
        (groups[key] if key in allowed else unexpected).append(run)
    rows = []
    for key in sorted(allowed):
        group = groups[key]
        complete = (len(group) == env['repeats'] and {r['repeat'] for r in group} == set(range(env['repeats'])))
        valid = bool(group) and all(valid_run(r) for r in group)
        row = dict(zip(('route', 'scenario', 'case'), key))
        row.update(runs=len(group), complete=complete, measurement_valid=valid,
                   failed_checks=[{'repeat': r['repeat'], 'checks': sorted(REQUIRED_CHECKS - r.get('measurement_checks', {}).keys()) +
                                  [name for name, ok in r.get('measurement_checks', {}).items() if ok is not True]}
                                  for r in group if not valid_run(r)])
        if valid:
            def median_metric(metric):
                return statistics.median(r[metric] for r in group)
            for metric in ('p95_ms', 'p99_ms', 'qps'):
                row[metric] = median_metric(metric)
            for name in (*COMPONENTS, 'total'):
                names = COMPONENTS if name == 'total' else (name,)
                row[name + '_cpu_s_per_million'] = statistics.median(
                    sum(r['resources'][n]['cpu_seconds'] for n in names) / r['completed'] * 1_000_000 for r in group)
                row[name + '_peak_rss_bytes'] = statistics.median(
                    sum(r['resources'][n]['peak_rss_bytes'] for n in names) for r in group)
        rows.append(row)
    indexed = {(r['route'], r['scenario'], r['case']): r for r in rows}
    comparisons = []
    for row in rows:
        references = ('baseline',) if row['case'] == 'upstream' else ('baseline', 'upstream') if row['case'] in ('cache', 'combined') else ()
        for reference in references:
            base = indexed.get((row['route'], row['scenario'], reference))
            if base is None:
                continue
            comparable = row['complete'] and base['complete'] and row['measurement_valid'] and base['measurement_valid']
            gates = {'complete_measurement': comparable}
            if comparable:
                for prefix in ('dnscrypt', 'total'):
                    gates[prefix + '_cpu'] = row[prefix + '_cpu_s_per_million'] <= base[prefix + '_cpu_s_per_million'] * 1.05
                    gates[prefix + '_rss'] = row[prefix + '_peak_rss_bytes'] <= base[prefix + '_peak_rss_bytes'] * 1.1
                gates['qps'] = row['qps'] >= base['qps'] * .95
                for metric in ('p95_ms', 'p99_ms'):
                    gates[metric] = row[metric] <= base[metric] + max(base[metric] * .05, .2)
            comparisons.append({'route': row['route'], 'scenario': row['scenario'], 'case': row['case'],
                                'reference': reference, 'gates': gates, 'passed': all(gates.values())})
    complete = not unexpected and all(r['complete'] for r in rows)
    valid = complete and all(r['measurement_valid'] for r in rows)
    all_screens = bool(comparisons) and all(c['passed'] for c in comparisons)
    return {'complete': complete, 'measurement_valid': valid,
            'prescreen_passed': valid and env['duration'] >= 30 and env['repeats'] >= 3 and all_screens,
            'configured_matrix_performance_passed': valid and env['duration'] >= 120 and env['repeats'] >= 5 and all_screens,
            'release_approved': False, 'unexpected_runs': len(unexpected), 'rows': rows, 'comparisons': comparisons,
            'limitations': ['Only the configured controlled matrix is covered, not all production rules or protocols.',
                            'RSS total is a sum of separately sampled process peaks, not an observed simultaneous peak.',
                            'Zero baseline component CPU has no useful percentage margin; inspect absolute counts.',
                            'Quota/resource validity must also be checked in the independent controller evidence.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(json.loads(args.results.read_text()))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('complete', 'measurement_valid', 'prescreen_passed', 'release_approved')}))


if __name__ == '__main__':
    main()
