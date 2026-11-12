"""Summarize matched DNS runs without treating short or incomplete runs as acceptance."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics

from dns_capture import METHOD
from dns_cpu import valid_placement
from dns_seed import valid_seed_path


def number(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def dns_resources(run, environment, field):
    names = (('dnscrypt',) if environment.get('entry') == 'dnscrypt' else
             ('redis', 'unbound', 'mosdns') if environment.get('entry') == 'mosdns' else ('redis', 'unbound'))
    values = [run.get('resources', {}).get(name, {}).get(field) for name in names]
    return sum(values) if all(number(v) for v in values) else None


def check_run(run, environment):
    duration, rate = environment.get('duration'), environment.get('qps')
    runtime, elapsed = run.get('run_time_seconds'), run.get('elapsed_seconds')
    sent, completed, lost = (run.get(name) for name in ('sent', 'completed', 'lost'))
    counts = (all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (sent, completed, lost))
              and completed > 0 and sent == completed + lost)
    codes = run.get('rcodes', {})
    counts = counts and bool(codes) and all(number(v) for v in codes.values()) and sum(codes.values()) == completed
    answers = run.get('answer_validation') or {}
    latencies = [run.get(name) for name in ('p50_ms', 'p95_ms', 'p99_ms')]
    trace_mode = environment.get('dnsperf_trace')
    trace_valid = True
    if trace_mode:
        trace = run.get('timing_trace') or {}
        trace_valid = (trace_mode in ('stream', 'memory') and trace.get('mode') == trace_mode
                       and trace.get('method') == 'dnsperf-loopback-paired-us-v1'
                       and trace.get('complete') is True and trace.get('samples') == completed
                       and answers.get('wire_latency', {}).get('complete') is True
                       and all(trace.get('client', {}).get(k) == run.get(k)
                               for k in ('p50_ms', 'p95_ms', 'p99_ms')))
    load_config = True
    if 'max_outstanding' in environment:
        limit = environment['max_outstanding']
        command = run.get('generator_command', [])
        load_config = (isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
                       and isinstance(command, list) and command.count('-q') == 1
                       and command.index('-q') + 1 < len(command)
                       and command[command.index('-q') + 1] == str(limit))
        load_config = load_config and all(run.get(key, {}).get('drops') == 0 for key in ('client_udp', 'server_udp'))
    cache_path = True
    if run.get('scenario') == 'cachedb':
        cachedb = run.get('cachedb_validation') or {}
        seed = cachedb.get('seed') or {}
        seed_answers = seed.get('answer_validation') or {}
        entries = cachedb.get('seed_queries')
        cache_path = (environment.get('entry', 'unbound') == 'unbound'
                      and number(entries, positive=True) and entries == cachedb.get('redis_entries')
                      and entries == seed.get('sent') == seed.get('completed')
                      and entries == seed.get('redis_entries')
                      and valid_seed_path(seed, entries, environment.get('cachedb_seed_protocol'))
                      and number(completed, positive=True) and completed <= entries
                      and seed.get('lost') == 0 and seed_answers.get('complete') is True
                      and seed_answers.get('method') == METHOD and not seed_answers.get('capture_error')
                      and seed_answers.get('errors') == seed_answers.get('capture_drops') == 0
                      and seed_answers.get('checked') == seed_answers.get('queries_seen') == entries
                      and seed.get('rcodes') == {'NOERROR': entries}
                      and seed.get('input_sha256') == run.get('input_sha256')
                      and isinstance(seed.get('input_sha256'), str)
                      and all(seed.get(key, {}).get('drops') == 0 for key in ('client_udp', 'server_udp'))
                      and all(number(cachedb.get(key), positive=True) for key in
                              ('seed_unbound_pid', 'measured_unbound_pid', 'seed_redis_pid', 'measured_redis_pid'))
                      and cachedb['seed_unbound_pid'] != cachedb['measured_unbound_pid']
                      and cachedb['seed_redis_pid'] == cachedb['measured_redis_pid']
                      and cachedb.get('redis_delta') == {'keyspace_hits': completed, 'keyspace_misses': 0}
                      and run.get('upstream_requests') == 0)
        if trace_mode:
            seed_trace = seed.get('timing_trace') or {}
            trace_valid = (trace_valid and seed_trace.get('mode') == trace_mode and seed_trace.get('complete') is True
                           and seed_trace.get('samples') == entries)
    encrypted_path = True
    if environment.get('entry') == 'dnscrypt':
        route = run.get('route_validation') or {}
        routes = route.get('doh_routes') or {}
        proxy = run.get('scenario') == 'doh-socks'
        expected, unexpected = ('proxy', 'direct') if proxy else ('direct', 'proxy')
        attempts = run.get('upstream_requests')
        connections = route.get('socks_connections')
        encrypted_path = (run.get('scenario') in ('doh-direct', 'doh-socks') and run.get('tls_verified') is True
                          and number(attempts) and number(completed, positive=True)
                          and completed <= attempts <= completed * 1.01
                          and number(routes.get(expected)) and routes[expected] >= attempts
                          and routes.get(unexpected) == routes.get('rejected') == 0
                          and number(connections) and (connections > 0 if proxy else connections == 0))
    return {
        'cpu_placement': (not environment.get('cpu_layout') or
                          (valid_placement(run.get('affinity_validation'), environment['cpu_layout'], environment.get('generator_thread_cpus'))
                           and (run.get('scenario') != 'cachedb' or valid_placement(
                               run.get('cachedb_validation', {}).get('seed', {}).get('affinity_validation'), environment['cpu_layout'],
                               environment.get('generator_thread_cpus'))))),
        'load_configuration': load_config,
        'encrypted_path': encrypted_path,
        'cache_path': cache_path,
        'trace': trace_valid,
        'serve_expired_timeout': run.get('serve_expired_client_timeout') == environment.get('serve_expired_client_timeout'),
        # dnsperf's runtime can include the final UDP timeout (2 seconds).
        'duration': (number(duration, positive=True) and number(runtime, positive=True) and number(elapsed, positive=True)
                     and duration - .01 <= runtime <= duration + 3 and elapsed >= runtime - .01),
        'offered_load': (counts and number(rate, positive=True) and number(duration, positive=True)
                         and rate * duration * .99 <= sent <= rate * duration * 1.01),
        'effective_qps': (counts and number(runtime, positive=True) and number(run.get('qps'), positive=True)
                          and number(rate, positive=True) and run['qps'] >= rate * .98
                          and math.isclose(run['qps'], completed / runtime, rel_tol=1e-5, abs_tol=.001)),
        'counts': counts and run.get('latency_samples') == completed,
        'latencies': all(number(v) for v in latencies) and latencies == sorted(latencies, key=lambda v: v or 0),
        'resources': all(dns_resources(run, environment, field) is not None for field in ('cpu_seconds', 'peak_rss_bytes')),
        'answers': (counts and answers.get('method') == METHOD and answers.get('complete') is True
                    and answers.get('queries_seen') == sent and answers.get('checked') == completed
                    and answers.get('errors') == 0 and answers.get('capture_drops') == 0 and not answers.get('capture_error')),
    }


def median(values):
    return statistics.median(values) if values and all(number(v) for v in values) else None


def summarize(payload):
    environment, runs = payload['environment'], payload['runs']
    groups = defaultdict(list)
    for run in runs:
        groups[(run['scenario'], run['case'])].append(run)
    expected = environment['repeats']
    rows = []
    for scenario in environment['scenarios']:
        for case in environment['cases']:
            group = groups[(scenario, case)]
            row = {'scenario': scenario, 'case': case, 'runs': len(group),
                   'complete': len(group) == expected and {r['repeat'] for r in group} == set(range(expected))}
            if group:
                checks = [check_run(r, environment) for r in group]
                row['invalid_runs'] = [{'repeat': r['repeat'], 'failed_checks': [name for name, ok in check.items() if not ok]}
                                       for r, check in zip(group, checks) if not all(check.values())]
                row['measurement_valid'] = all(all(check.values()) for check in checks)
                row['actual_duration_met'] = all(check['duration'] for check in checks)
                row['offered_load_met'] = all(check['offered_load'] and check['effective_qps'] for check in checks)
                row['answers_verified'] = all(check['answers'] for check in checks)
                row.update({name: median([r.get(name) for r in group])
                            for name in ('qps', 'p50_ms', 'p95_ms', 'p99_ms')})
                row['lost'] = sum(r['lost'] for r in group)
                row['unexpected_rcodes'] = sorted({code for r in group for code in r['rcodes'] if code != 'NOERROR'})
                row['dns_cpu_seconds_per_million'] = median([
                    dns_resources(r, environment, 'cpu_seconds') * 1_000_000 / r['completed']
                    if dns_resources(r, environment, 'cpu_seconds') is not None and number(r.get('completed'), positive=True) else None
                    for r in group])
                row['dns_peak_rss_bytes'] = median([dns_resources(r, environment, 'peak_rss_bytes') for r in group])
                row['upstream_requests'] = sum(r['upstream_requests'] for r in group)
                completed = sum(r['completed'] for r in group)
                row['actual_upstream_ratio'] = row['upstream_requests'] / completed if completed else None
            rows.append(row)
    by_key = {(r['scenario'], r['case']): r for r in rows}
    for row in rows:
        base = by_key.get((row['scenario'], 'baseline'))
        if row['case'] == 'baseline' or not base or not base['runs'] or not row['runs']:
            continue
        valid = row['measurement_valid'] and base['measurement_valid']
        gates = {'measurement': valid, 'qps': valid and row['qps'] >= base['qps'] * .95,
                 'cpu': valid and row['dns_cpu_seconds_per_million'] <= base['dns_cpu_seconds_per_million'] * 1.05,
                 'peak_rss_screen': valid and row['dns_peak_rss_bytes'] <= base['dns_peak_rss_bytes'] * 1.1}
        for metric in ('p95_ms', 'p99_ms'):
            gates[metric] = valid and row[metric] <= base[metric] + max(base[metric] * .05, .2)
        gates['correctness'] = row['answers_verified'] and row['lost'] == 0 and not row['unexpected_rcodes']
        row['screens'] = gates
    complete = all(r['complete'] for r in rows)
    adequate = complete and environment['duration'] >= 120 and expected >= 5 and all(r.get('actual_duration_met') for r in rows)
    measured = complete and all(r.get('measurement_valid') for r in rows)
    compared = [r for r in rows if 'screens' in r]
    correct = all(r.get('answers_verified') and not r.get('lost') and not r.get('unexpected_rcodes') for r in rows)
    return {'scope': ('Controlled DNSCrypt TLS DoH direct/SOCKS paths' if environment.get('entry') == 'dnscrypt' else
                     'Frozen MosDNS cache/forward entrance' if environment.get('entry') == 'mosdns' else 'Controlled Unbound and Redis backend')
                     + '; fixed offered QPS, not maximum throughput; controlled forwarding does not exercise production splitting rules',
            'complete': complete, 'duration_and_repeats_met': adequate, 'measurement_valid': measured,
            'screen_passed': complete and adequate and measured and correct and bool(compared) and all(all(r['screens'].values()) for r in compared),
            'limitations': ['Peak RSS is a screening value; investigate changes above 10% and inspect steady-state trends.',
                            'Median comparisons require stable background load; inspect all raw runs and resource samples.',
                            'Unmeasured scenarios and final soak/observation are not certified by this report.'],
            'environment': environment, 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(json.loads(args.results.read_text()))
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('complete', 'duration_and_repeats_met', 'screen_passed')}))


if __name__ == '__main__':
    main()
