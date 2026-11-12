"""Finite scheduling diagnostics; never a component performance approval."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import subprocess
import sys
import threading
import time

from dns_lab import Fixture
from dns_scheduling import SchedulingMonitor, trace_summary

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def watch(work, duration):
    """Independent 10 ms heartbeat; records gaps without filtering failures."""
    started, previous = time.monotonic_ns(), None
    rows = []
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    while not stopped.is_set() and time.monotonic_ns() - started < duration * 1_000_000_000:
        now = time.monotonic_ns()
        rows.append({'monotonic_ns': now, 'unix_ns': time.time_ns(),
                     'gap_ns': now - previous if previous is not None else 0})
        previous = now
        stopped.wait(.01)
    (work / 'heartbeat.json').write_text(json.dumps({'diagnostic_only': True, 'affinity': sorted(os.sched_getaffinity(0)),
                                                   'duration': duration, 'stopped_by_controller': stopped.is_set(), 'rows': rows}) + '\n')
    return {'samples': len(rows), 'max_gap_ns': max(row['gap_ns'] for row in rows),
            'stopped_by_controller': stopped.is_set()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('loopback', 'dnscrypt', 'intrinsic', 'watch'), required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--generator', type=Path)
    parser.add_argument('--expected-generator-sha256')
    parser.add_argument('--trace', action='store_true')
    parser.add_argument('--baseline-home', type=Path)
    parser.add_argument('--candidates', type=Path)
    args = parser.parse_args()
    limit = 600 if args.mode == 'watch' else 120
    if args.work.exists() or not 5 <= args.duration <= limit:
        parser.error('Use a fresh work directory and a bounded diagnostic duration')
    if args.mode != 'watch' and Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Use a disposable network namespace with loopback enabled')
    if args.mode in ('loopback', 'dnscrypt'):
        if not args.generator or not args.expected_generator_sha256:
            parser.error('Pin the exact diagnostic/reference generator SHA-256')
        if digest(args.generator) != args.expected_generator_sha256:
            raise ValueError('Generator SHA-256 mismatch')
    if args.mode in ('intrinsic', 'dnscrypt') and not args.baseline_home:
        parser.error('The deployed baseline directory is required')
    if args.mode == 'dnscrypt' and not args.candidates:
        parser.error('The verified DoH fixture directory is required')
    args.work = args.work.resolve()
    args.work.mkdir(mode=0o700, parents=True)
    environment = {'diagnostic_only': True, 'performance_accepted': False, 'mode': args.mode,
                   'duration': args.duration, 'affinity': sorted(os.sched_getaffinity(0)),
                   'nice': os.getpriority(os.PRIO_PROCESS, 0),
                   'generator_sha256': args.expected_generator_sha256, 'send_trace': args.trace,
                   'method_sha256': {name: digest(ROOT / 'scripts' / name) for name in
                                     ('diagnose-dns-scheduling.py', 'dns_scheduling.py', 'benchmark-dns.py', 'dns_capture.py')}}
    (args.work / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    result = {'diagnostic_only': True, 'performance_accepted': False, 'completed': False, 'environment': environment}
    started = time.monotonic()
    try:
        if args.mode == 'watch':
            result['heartbeat'] = watch(args.work, args.duration)
        elif args.mode == 'intrinsic':
            command = [str((args.baseline_home / 'core/redis-cli').resolve()), '--intrinsic-latency', str(args.duration)]
            result['command'] = command
            with SchedulingMonitor(args.work / 'scheduling.jsonl', max_seconds=args.duration + 15):
                with (args.work / 'intrinsic.log').open('xb') as stream:
                    subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=args.duration + 15)
            raw = (args.work / 'intrinsic.log').read_text()
            maxima = [int(value) for value in re.findall(r'Max latency so far: (\d+) microseconds', raw)]
            if not maxima:
                raise ValueError('No intrinsic latency samples were reported')
            result['intrinsic_max_us'] = max(maxima)
        else:
            generator = args.generator.resolve()
            if args.trace:
                wrapper = args.work / 'traced-generator'
                wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(str(generator)) + ' "$@" -trace ' +
                                   shlex.quote(str(args.work / 'send-trace.json')) + '\n')
                wrapper.chmod(0o700)
                generator = wrapper
            with SchedulingMonitor(args.work / 'scheduling.jsonl', max_seconds=args.duration + 60):
                if args.mode == 'loopback':
                    fixture = Fixture(ttl=600, threaded_udp=False)
                    try:
                        data = args.work / 'questions.txt'
                        data.write_text('\n'.join(f'cold-{i}.cache.test ' + ('AAAA' if i % 5 == 0 else 'A')
                                                  for i in range(100 * args.duration + 10)) + '\n')
                        bench = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))
                        row = bench['load'](generator, fixture.port, data, args.duration, 100, args.work / 'load.txt',
                                            {'fixture': os.getpid()}, {1: '192.0.2.10', 28: '2001:db8::10'}, max_outstanding=64)
                        pacing = runpy.run_path(str(ROOT / 'scripts/benchmark-dnscrypt-source.py'))['pacing_evidence']
                        row['pacing_validation'] = pacing(row, 100)
                        result['run'] = row
                        result['answers_valid'] = (row['sent'] == row['completed'] and row['lost'] == 0 and
                                                   row['answer_validation']['complete'] and row['answer_validation']['errors'] == 0)
                    finally:
                        fixture.close()
                else:
                    # The compatible load-generator interface lets this diagnostic
                    # wrapper reuse the exact old resolver/HTTP2/SOCKS setup. Only
                    # baseline is selected; these instrumented runs are never A/B.
                    command = [sys.executable, str(ROOT / 'scripts/benchmark-dnscrypt-source.py'),
                               '--baseline-home', str(args.baseline_home.resolve()), '--candidates', str(args.candidates.resolve()),
                               '--work', str(args.work / 'baseline'), '--duration', str(args.duration), '--repeats', '1',
                               '--cases', 'baseline', '--protocols', 'h2', '--scenarios', 'doh-socks',
                               '--load-generator', 'dnsperf', '--dnsperf', str(generator)]
                    result['command'] = command
                    with (args.work / 'baseline.log').open('xb') as stream:
                        child = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=args.duration + 60)
                    result['benchmark_exit_code'] = child.returncode
                    path = args.work / 'baseline/results.json'
                    if not path.exists():
                        raise RuntimeError('Baseline diagnostic stopped before recording a measured run')
                    payload = json.loads(path.read_text())
                    if len(payload['runs']) != 1:
                        raise ValueError('Expected exactly one diagnostic baseline run')
                    payload['environment']['diagnostic_only'] = True
                    payload['runs'][0]['diagnostic_only'] = True
                    path.write_text(json.dumps(payload, indent=2) + '\n')
                    result['run'] = payload['runs'][0]
                    result['answers_valid'] = result['run']['measurement_checks']['answers']
            if args.trace:
                trace = json.loads((args.work / 'send-trace.json').read_text())
                result['send_trace'] = trace_summary(trace)
                if len(trace['rows']) != result['run']['sent']:
                    raise ValueError('Send trace count differs from captured queries')
        result['completed'] = True
    except BaseException as error:
        result['error'] = repr(error)
        raise
    finally:
        result['elapsed'] = time.monotonic() - started
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({key: value for key, value in result.items() if key not in ('environment', 'run', 'send_trace', 'command')}), flush=True)


if __name__ == '__main__':
    main()
