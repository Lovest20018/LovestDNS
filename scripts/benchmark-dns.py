"""Run controlled, repeatable DNS component comparisons with dnsperf samples."""
import argparse
import contextlib
from collections import Counter
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import runpy
import socket
import subprocess
import time

from dns_lab import Fixture, FixtureProcess, candidate_command, query, running_lab
from dns_capture import WIRE_RECORD, CaptureProcess, client_udp_sample
from dns_cpu import cpu_list, parse_generator_threads, parse_layout, pin, placed
from dns_seed import PROTOCOL, coverage, valid_seed_path
from dnsperf_trace import MAX_TRACE, RCODES, read_client, read_thread_affinity, split_timings, timing_summary

ROOT = Path(__file__).resolve().parents[1]


def percentile(samples, fraction):
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def parse_dnsperf(text, trace=None, mode=None):
    samples = re.findall(r'^>\s+(\S+)\s+\S+\s+\S+\s+(\d+(?:\.\d+)?)\s*$', text, re.M)
    def field(name):
        match = re.search(r'^\s*' + re.escape(name) + r':\s+([\d.]+)', text, re.M)
        if not match:
            raise ValueError('Missing dnsperf field: ' + name)
        return float(match[1])
    sent, completed, lost = (int(field(x)) for x in ('Queries sent', 'Queries completed', 'Queries lost'))
    if mode:
        trace_samples = [(RCODES[code], received - sent_at) for _, sent_at, received, code in trace]
        if mode == 'stream' and trace_samples != [(code, round(float(s) * 1e6)) for code, s in samples]:
            raise ValueError('dnsperf live log differs from the in-memory timestamp records')
        if mode == 'memory' and samples:
            raise ValueError('Deferred recording unexpectedly produced live per-query log lines')
        codes = Counter(code for code, _ in trace_samples)
        latencies = [micros / 1000 for _, micros in trace_samples]
    else:
        codes = Counter(x[0] for x in samples)
        latencies = [float(x[1]) * 1000 for x in samples]
    if len(latencies) != completed or sent != completed + lost:
        raise ValueError('Per-query latency sample count differs from completed queries')
    return {'sent': sent, 'completed': completed, 'lost': lost, 'qps': field('Queries per second'),
            'run_time_seconds': field('Run time (s)'),
            'timeouts_reported': len(re.findall(r'^> T\s', text, re.M)),
            'rcodes': dict(codes), 'latency_samples': len(latencies),
            'p50_ms': percentile(latencies, .5), 'p95_ms': percentile(latencies, .95),
            'p99_ms': percentile(latencies, .99)}


def process_sample(pid):
    try:
        root = Path('/proc') / str(pid)
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        pages = int((root / 'statm').read_text().split()[1])
        return {'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                'rss_bytes': pages * os.sysconf('SC_PAGE_SIZE')}
    except (OSError, ValueError, IndexError):
        return None


def process_affinity(pid):
    try:
        return sorted(os.sched_getaffinity(pid))
    except ProcessLookupError:
        return None


def fixture_pid(fixture):
    return fixture.process.pid if isinstance(fixture, FixtureProcess) else os.getpid()


def load(dnsperf, port, data, duration, qps, path, processes, expected, *, max_outstanding=256, cpu_layout=None,
         trace_mode=None, scheduler_trace=False, thread_cpus=None):
    cpu_layout = cpu_layout or {}
    thread_cpus = thread_cpus or {}
    if thread_cpus and (not trace_mode or set(thread_cpus.values()) != set(cpu_list(cpu_layout.get('generator')))):
        raise ValueError('Per-thread placement requires the timestamp recorder and both generator CPUs')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(('127.0.0.1', 0))
        client_port = reservation.getsockname()[1]
    command = [str(dnsperf), '-s', '127.0.0.1', '-p', str(port), '-d', str(data),
               '-a', '127.0.0.1', '-x', str(client_port), '-m', 'udp',
               '-l', str(duration), '-Q', str(qps), '-q', str(max_outstanding), '-t', '2', '-n', '1', '-v', '-W']
    command = placed(command, cpu_layout.get('generator'))
    path = Path(path)
    capture_args = (port, client_port, data, expected)
    env = None
    if trace_mode:
        capacity = math.ceil(duration * qps * 1.02) + 64
        if capacity > MAX_TRACE:
            raise ValueError('Requested timestamp trace exceeds the bounded capacity')
        client_trace = path.with_suffix('.client.tsv').resolve()
        wire_trace = path.with_suffix('.wire.bin').resolve()
        capture_args += (wire_trace, capacity)
        clean_env = {key: value for key, value in os.environ.items()
                     if key not in ('DNSPERF_RECV_CPU', 'DNSPERF_SEND_CPU', 'DNSPERF_AFFINITY_FILE')}
        env = {**clean_env, 'DNSPERF_TRACE_FILE': str(client_trace),
               'DNSPERF_TRACE_MODE': trace_mode, 'DNSPERF_TRACE_CAPACITY': str(capacity)}
        if thread_cpus:
            thread_audit = path.with_suffix('.affinity.tsv').resolve()
            env.update(DNSPERF_RECV_CPU=str(thread_cpus['receiver']), DNSPERF_SEND_CPU=str(thread_cpus['sender']),
                       DNSPERF_AFFINITY_FILE=str(thread_audit))
    samples = []
    udp = {'port': client_port, 'peak_rx_queue_bytes': 0, 'drops': 0}
    server_before = client_udp_sample(port)
    if server_before is None:
        raise RuntimeError('Resolver UDP socket counters unavailable')
    server_udp = {'port': port, 'peak_rx_queue_bytes': 0, 'drops': 0}
    affinity = {'samples': 0, 'mismatches': [], 'expected': cpu_layout, 'last': {},
                'generator_threads_checked': 0, 'thread_mismatches': [], 'thread_observations': {}}
    scheduler = contextlib.nullcontext()
    if scheduler_trace:
        from dns_scheduler_trace import ReceiverTrace
        if trace_mode != 'memory' or cpu_list(cpu_layout.get('generator', [])) != [13, 14]:
            raise ValueError('Receiver trace requires memory recording on generator CPUs 13,14')
        scheduler = ReceiverTrace(path.with_suffix('.scheduler'))
    with path.open('wb') as stream, CaptureProcess(*capture_args, cpu=cpu_layout.get('capture')) as capture, scheduler:
        started = time.monotonic()
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        all_processes = {**processes, 'generator': child.pid, 'answer_capture': capture.process.pid}
        first = {name: process_sample(pid) for name, pid in all_processes.items()}
        peak = {name: 0 for name in all_processes}
        latest = dict(first)
        try:
            while child.poll() is None:
                if scheduler_trace:
                    scheduler.observe(child.pid)
                if cpu_layout and time.monotonic() - started >= .1:
                    observed = {name: value for name, pid in all_processes.items()
                                if (value := process_affinity(pid)) is not None}
                    observed['controller'] = sorted(os.sched_getaffinity(0))
                    expected_affinity = {**cpu_layout, 'answer_capture': cpu_layout['capture']}
                    bad = {name: value for name, value in observed.items()
                           if value != cpu_list(expected_affinity[name])}
                    threads_checked = 0
                    for task in Path('/proc', str(child.pid), 'task').glob('[0-9]*'):
                        thread_mask = process_affinity(int(task.name))
                        if thread_mask is not None:
                            try:
                                name = (task / 'comm').read_text().strip()
                                fields = (task / 'stat').read_text().rsplit(')', 1)[1].split()
                                last_cpu = int(fields[36])
                            except (OSError, IndexError, ValueError):
                                continue
                            role = {'perf-recv-0000': 'receiver', 'perf-send-0000': 'sender'}.get(name)
                            expected_mask = ([thread_cpus[role]] if role in thread_cpus else cpu_list(cpu_layout['generator']))
                            threads_checked += 1
                            if thread_mask != expected_mask or (role in thread_cpus and last_cpu != thread_cpus[role]):
                                affinity['thread_mismatches'].append({'tid': int(task.name), 'cpus': thread_mask})
                            if role in thread_cpus:
                                seen = affinity['thread_observations'].setdefault(role, {'tid': int(task.name), 'samples': 0})
                                if seen['tid'] != int(task.name):
                                    affinity['thread_mismatches'].append({'role': role, 'changed_tid': int(task.name)})
                                seen.update(cpus=thread_mask, last_cpu=last_cpu, samples=seen['samples'] + 1)
                    affinity['generator_threads_checked'] = max(affinity['generator_threads_checked'], threads_checked)
                    affinity['samples'] += 1
                    affinity['last'].update(observed)
                    if bad:
                        affinity['mismatches'].append({'seconds': time.monotonic() - started, 'roles': bad})
                now = {name: process_sample(pid) for name, pid in all_processes.items()}
                for name, sample in now.items():
                    if sample:
                        latest[name] = sample
                        peak[name] = max(peak[name], sample['rss_bytes'])
                queue = client_udp_sample(client_port)
                if queue:
                    udp['peak_rx_queue_bytes'] = max(udp['peak_rx_queue_bytes'], queue['rx_queue_bytes'])
                    udp['drops'] = max(udp['drops'], queue['drops'])
                server_queue = client_udp_sample(port)
                if server_queue:
                    server_udp['peak_rx_queue_bytes'] = max(server_udp['peak_rx_queue_bytes'], server_queue['rx_queue_bytes'])
                    server_udp['drops'] = max(server_udp['drops'], server_queue['drops'] - server_before['drops'])
                samples.append({'seconds': time.monotonic() - started, 'processes': now,
                                'client_udp': queue, 'server_udp': server_queue})
                if time.monotonic() - started > duration + 15:
                    raise TimeoutError('dnsperf exceeded bounded duration')
                time.sleep(.5)
            if child.returncode:
                raise RuntimeError(path.read_text(errors='replace')[-2000:])
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
    elapsed = time.monotonic() - started
    client = read_client(client_trace, capacity) if trace_mode else None
    result = parse_dnsperf(path.read_text(), client, trace_mode)
    result['answer_validation'] = capture.result(result['sent'], result['completed'])
    if trace_mode:
        wire_bytes = wire_trace.read_bytes()
        if (len(wire_bytes) != result['completed'] * WIRE_RECORD.size or result['lost']
                or not result['answer_validation']['wire_latency']['complete']):
            raise ValueError('Incomplete packet pairing in dnsperf timestamp trace')
        paired = split_timings(client, WIRE_RECORD.iter_unpack(wire_bytes))
        result['timing_trace'] = {**timing_summary(paired), 'mode': trace_mode}
    result['client_udp'] = udp
    server_after = client_udp_sample(port)
    if server_after is None:
        raise RuntimeError('Resolver UDP socket counters disappeared')
    server_udp['drops'] = max(server_udp['drops'], server_after['drops'] - server_before['drops'])
    result['server_udp'] = server_udp
    result['generator_command'] = command
    if cpu_layout:
        roles = {'redis', 'unbound', 'fixture', 'generator', 'answer_capture', 'controller'}
        affinity['complete'] = (affinity['samples'] > 0 and not affinity['mismatches'] and not affinity['thread_mismatches']
                                and affinity['generator_threads_checked'] >= 2 and set(affinity['last']) == roles)
        if thread_cpus:
            affinity['thread_preflight'] = read_thread_affinity(thread_audit, thread_cpus, client)
            affinity['complete'] &= set(affinity['thread_observations']) == set(thread_cpus)
        result['affinity_validation'] = affinity
    result['resources'] = {name: {'cpu_seconds': max(0, latest[name]['cpu_seconds'] - first[name]['cpu_seconds'])
                                  if first[name] and latest[name] else None,
                                  'peak_rss_bytes': peak[name]} for name in all_processes}
    result['elapsed_seconds'] = elapsed
    result['generator_sha256'] = hashlib.sha256(Path(dnsperf).read_bytes()).hexdigest()
    result['input_sha256'] = hashlib.sha256(Path(data).read_bytes()).hexdigest()
    path.with_suffix('.resources.json').write_text(json.dumps(samples) + '\n')
    with path.open('rb') as src, gzip.open(str(path) + '.gz', 'wb') as dst:
        import shutil
        shutil.copyfileobj(src, dst)
    path.unlink()
    return result


def redis_counters(lab):
    values = dict(line.split(':', 1) for line in lab.cli('INFO', 'stats').splitlines() if ':' in line)
    return {key: int(values[key]) for key in ('keyspace_hits', 'keyspace_misses')}


def prepare_cachedb(lab, fixture, dnsperf, data, qps, max_outstanding, trace_mode=None, thread_cpus=None):
    """Populate each unique question, then restart only Unbound before measuring."""
    count = len(data.read_text().splitlines())
    rate = min(qps, 500)
    before = fixture.requests()
    question_before = fixture.question_snapshot()
    expected = {(name.rstrip('.').lower(), {'A': 1, 'AAAA': 28}[kind], 1)
                for name, kind in (line.split() for line in data.read_text().splitlines())}
    if len(expected) != count:
        raise ValueError('Cachedb seed input must contain unique DNS questions')
    outcome = load(dnsperf, lab.ports[0], data, math.ceil(count / rate) + 1, rate,
                   lab.root / 'cachedb-seed.txt',
                   {'redis': lab.redis.process.pid, 'unbound': lab.unbounds[0].process.pid,
                    'fixture': fixture_pid(fixture)}, {1: fixture.address, 28: '2001:db8::10'},
                   max_outstanding=max_outstanding, cpu_layout=lab.cpu_layout, trace_mode=trace_mode, thread_cpus=thread_cpus)
    outcome['upstream_requests'] = fixture.requests() - before
    outcome['redis_entries'] = int(lab.cli('DBSIZE'))
    question_after = fixture.question_snapshot()
    outcome['question_coverage'] = coverage(expected, question_before, question_after, outcome['input_sha256'])
    (lab.root / 'cachedb-seed-questions.json').write_text(
        json.dumps(sorted([*key, count] for key, count in (question_after - question_before).items()),
                   separators=(',', ':')) + '\n')
    (lab.root / 'cachedb-seed.json').write_text(json.dumps(outcome, indent=2) + '\n')
    validation = outcome['answer_validation']
    if (outcome['sent'] != count or outcome['completed'] != count or outcome['lost']
            or not validation['complete'] or validation['errors']
            or outcome['client_udp']['drops'] or outcome['server_udp']['drops']
            or not valid_seed_path(outcome, count, PROTOCOL) or outcome['redis_entries'] != count):
        raise RuntimeError('Redis seed did not populate every unique DNS question correctly')
    old_pid = lab.unbounds[0].process.pid
    redis_pid = lab.redis.process.pid
    lab.restart_unbounds()
    return {'seed': outcome, 'seed_queries': count, 'seed_unbound_pid': old_pid,
            'measured_unbound_pid': lab.unbounds[0].process.pid,
            'seed_redis_pid': redis_pid, 'measured_redis_pid': lab.redis.process.pid,
            'redis_entries': int(lab.cli('DBSIZE'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-home', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--dnsperf', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--qps', type=int, default=1000)
    parser.add_argument('--max-outstanding', type=int, default=256,
                        help='Same dnsperf in-flight limit for every compared component case')
    parser.add_argument('--scenarios', default='hot,mixed90,mixed50,mixed10')
    parser.add_argument('--cases', default='baseline,redis-only,unbound-only,combined')
    parser.add_argument('--command-timeout', type=int, help='Optional candidate-only Unbound command timeout experiment')
    parser.add_argument('--serve-expired-client-timeout', type=int, help='Explicit Unbound stale-answer timer in ms')
    parser.add_argument('--dnsperf-trace', choices=['stream', 'memory'],
                        help='Requires a dnsperf binary with the experimental timestamp recorder')
    parser.add_argument('--receiver-scheduler-trace', action='store_true', help='Finite filtered ftrace diagnostic only')
    parser.add_argument('--generator-thread-cpus', help='Pin dnsperf before timing: receiver=N,sender=N')
    parser.add_argument('--entry', choices=['unbound', 'mosdns'], default='unbound')
    parser.add_argument('--fixture-udp-mode', choices=['threaded', 'serial'], default='threaded',
                        help='Serial responses avoid creating a thread for each controlled upstream UDP packet')
    parser.add_argument('--cpu-layout', help='Distinct allowed CPUs as controller=N,fixture=N,redis=N,unbound=N,capture=N,generator=N+N')
    args = parser.parse_args()
    try:
        cpu_layout = parse_layout(args.cpu_layout, os.sched_getaffinity(0))
        thread_cpus = parse_generator_threads(args.generator_thread_cpus, cpu_layout)
    except ValueError as error:
        parser.error(str(error))
    if cpu_layout and args.entry != 'unbound':
        parser.error('Role CPU placement currently requires the Unbound entrance')
    if args.dnsperf_trace and (args.entry != 'unbound' or not cpu_layout):
        parser.error('The timestamp recorder requires the isolated Unbound CPU layout')
    if thread_cpus and not args.dnsperf_trace:
        parser.error('Generator thread placement requires the timestamp recorder')
    if args.receiver_scheduler_trace and (args.dnsperf_trace != 'memory' or args.duration > 60):
        parser.error('Receiver trace requires memory recording and at most 60 seconds')
    if args.serve_expired_client_timeout is not None and args.serve_expired_client_timeout < 0:
        parser.error('The stale-answer timeout must be nonnegative')
    if args.work.exists() or min(args.duration, args.repeats, args.qps, args.max_outstanding) < 1:
        parser.error('Use a new work directory and positive limits')
    cases = {'baseline': (False, False), 'redis-only': (True, False), 'unbound-only': (False, True),
             'combined': (True, True), 'combined-20ms': (True, True)}
    chosen = args.cases.split(',')
    scenarios = args.scenarios.split(',')
    if set(chosen) - set(cases) or set(scenarios) - {'hot', 'mixed90', 'mixed50', 'mixed10', 'cold', 'cachedb'}:
        parser.error('Unsupported scenario or case')
    if 'cachedb' in scenarios:
        preparation = math.ceil((args.qps * args.duration + 10) / min(args.qps, 500)) + 1
        if args.entry != 'unbound' or preparation + args.duration > 480:
            parser.error('cachedb requires the Unbound entrance and preparation/load well within the 600s TTL')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    manifest = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    args.work.mkdir(parents=True)
    old = {name: [str((args.baseline_home / 'core' / name).resolve())] for name in ('redis-server', 'redis-cli', 'unbound')}
    new = {name: candidate_command(args.bundle, 'unbound' if name == 'unbound' else 'redis', name) for name in old}
    environment = {'kernel': os.uname().release, 'cpu_count': os.cpu_count(), 'loadavg_start': os.getloadavg(),
                   'architecture': manifest['architecture'], 'lock_sha256': manifest['lock_sha256'],
                   'tool_sha256': hashlib.sha256(args.dnsperf.read_bytes()).hexdigest(),
                   'duration': args.duration, 'repeats': args.repeats, 'qps': args.qps, 'cases': chosen, 'scenarios': scenarios,
                   'max_outstanding': args.max_outstanding,
                   'baseline_home': str(args.baseline_home.resolve()), 'command_timeout': args.command_timeout,
                   'serve_expired_client_timeout': args.serve_expired_client_timeout, 'dnsperf_trace': args.dnsperf_trace,
                   'receiver_scheduler_trace': args.receiver_scheduler_trace,
                   'generator_thread_cpus': thread_cpus,
                   'affinity': sorted(os.sched_getaffinity(0)), 'entry': args.entry,
                   'fixture_udp_mode': args.fixture_udp_mode,
                   'cpu_layout': cpu_layout,
                   'fixture_process': bool(cpu_layout),
                   'cachedb_seed_protocol': PROTOCOL,
                   'method_sha256': {name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                                     for name in ('benchmark-dns.py', 'dns_lab.py', 'dns_cpu.py', 'dns_seed.py', 'dns_capture.py',
                                                  'dnsperf_trace.py', 'report-dns-benchmark.py',
                                                  *(('dns_scheduler_trace.py',) if args.receiver_scheduler_trace else ()))}}
    (args.work / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    results = []
    pin(cpu_layout.get('controller'))
    fixture_arguments = dict(ttl=600, threaded_udp=args.fixture_udp_mode == 'threaded', track_questions='cachedb' in scenarios)
    fixture = (FixtureProcess(cpu=cpu_layout['fixture'], **fixture_arguments) if cpu_layout else Fixture(**fixture_arguments))
    try:
        for scenario in scenarios:
            ratio = 1 if scenario == 'hot' else 0 if scenario in ('cold', 'cachedb') else int(scenario.removeprefix('mixed')) / 100
            data = args.work / (scenario + '.txt')
            rng = random.Random(20261107)
            rows = []
            total = args.qps * args.duration + 10
            for i in range(total):
                name = f'hot-{rng.randrange(256)}.cache.test' if rng.random() < ratio else f'cold-{i}.cache.test'
                rows.append(name + (' AAAA' if i % 5 == 0 else ' A'))
            data.write_text('\n'.join(rows) + '\n')
            for repeat in range(args.repeats):
                order = chosen if repeat % 2 == 0 else list(reversed(chosen))
                for label in order:
                    redis_new, unbound_new = cases[label]
                    redis, unbound = new if redis_new else old, new if unbound_new else old
                    work = args.work / f'{scenario}-{repeat}-{label}'
                    timeout = 20 if label == 'combined-20ms' else args.command_timeout if unbound_new else None
                    with running_lab(work, redis['redis-server'], redis['redis-cli'], unbound['unbound'], [fixture],
                                     command_timeout=timeout, cpu_layout=cpu_layout,
                                     serve_expired_client_timeout=args.serve_expired_client_timeout) as lab:
                        target = lab.ports[0]
                        if args.entry == 'mosdns':
                            frozen = args.baseline_home / 'libexec/mosdns'
                            if hashlib.sha256(frozen.read_bytes()).hexdigest() != lock['frozen']['mosdns']['sha256']:
                                raise ValueError('MosDNS entrance differs from frozen baseline')
                            target = lab.mosdns_proxy([str((args.baseline_home / 'core/mosdns').resolve())])
                        cachedb = None
                        if scenario == 'cachedb':
                            cachedb = prepare_cachedb(lab, fixture, args.dnsperf.resolve(), data.resolve(),
                                                     args.qps, args.max_outstanding, args.dnsperf_trace, thread_cpus)
                            counters = redis_counters(lab)
                        else:
                            for i in range(256):
                                for qtype in (1, 28):
                                    answer = query(target, f'hot-{i}.cache.test', qtype=qtype)
                                    expected = fixture.address if qtype == 1 else '2001:db8::10'
                                    assert answer['rcode'] == 0 and answer['answers'][0]['value'] == expected, answer
                        before = fixture.requests()
                        pids = {'redis': lab.redis.process.pid, 'unbound': lab.unbounds[0].process.pid,
                                'fixture': fixture_pid(fixture)}
                        if args.entry == 'mosdns':
                            pids['mosdns'] = lab.mosdns.process.pid
                        outcome = load(args.dnsperf.resolve(), target, data.resolve(), args.duration, args.qps,
                                       work / 'dnsperf.txt', pids, {1: fixture.address, 28: '2001:db8::10'},
                                       max_outstanding=args.max_outstanding, cpu_layout=cpu_layout,
                                       trace_mode=args.dnsperf_trace, scheduler_trace=args.receiver_scheduler_trace, thread_cpus=thread_cpus)
                        outcome.update({'scenario': scenario, 'case': label, 'repeat': repeat,
                                        'upstream_requests': fixture.requests() - before,
                                        'redis_info': {section: lab.cli('INFO', section) for section in ('stats', 'memory', 'persistence')},
                                        'command_timeout': timeout, 'loadavg_end': os.getloadavg(),
                                        'serve_expired_client_timeout': args.serve_expired_client_timeout})
                        if cachedb is not None:
                            after = redis_counters(lab)
                            cachedb['redis_delta'] = {key: after[key] - counters[key] for key in counters}
                            outcome['cachedb_validation'] = cachedb
                        results.append(outcome)
                        (args.work / 'results.json').write_text(json.dumps({'environment': environment, 'runs': results}, indent=2) + '\n')
                        print(json.dumps({k: outcome[k] for k in ('scenario', 'case', 'repeat', 'qps', 'p50_ms', 'p95_ms', 'p99_ms', 'lost')}), flush=True)
                        assert outcome['lost'] == 0 and set(outcome['rcodes']) == {'NOERROR'}, outcome
                        assert outcome['answer_validation']['complete'] and outcome['answer_validation']['errors'] == 0, outcome
                        if cachedb is not None:
                            assert (outcome['upstream_requests'] == 0
                                    and cachedb['redis_delta']['keyspace_hits'] == outcome['completed']
                                    and cachedb['redis_delta']['keyspace_misses'] == 0), outcome
    finally:
        fixture.close()


if __name__ == '__main__':
    main()
