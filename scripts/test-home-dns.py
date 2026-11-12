"""Finite ten-client functional soak of the selected DNS chain in private loopback."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import signal
import socket
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

from dns_lab import Child, Fixture, exact, free_port, parse, question, running_lab

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def ask(port, name, kind, *, tcp=False, ipv6=False):
    packet = question(name, kind)
    began = time.monotonic_ns()
    with socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET,
                       socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect(('::1' if ipv6 else '127.0.0.1', port))
        if tcp:
            client.sendall(struct.pack('!H', len(packet)) + packet)
            response = exact(client, struct.unpack('!H', exact(client, 2))[0])
        else:
            client.send(packet)
            response = client.recv(65535)
    elapsed = (time.monotonic_ns() - began) / 1e6
    answer = parse(response)
    # Verify the echoed question as well as the transaction and complete answer.
    if (response[12:12 + len(packet) - 12] != packet[12:]
            or answer['id'] != int.from_bytes(packet[:2], 'big') or answer['rcode'] or answer['truncated']
            or len(answer['answers']) != 1):
        raise ValueError('DNS transaction, question or response status differs')
    value = answer['answers'][0]
    if (value['name'].lower().rstrip('.') != name.lower().rstrip('.') or value['type'] != kind
            or value['class'] != 1 or not 0 < value['ttl'] <= 600
            or value['value'] != ('192.0.2.10' if kind == 1 else '2001:db8::10')):
        raise ValueError('DNS answer, type, address or TTL differs')
    return elapsed


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'candidates', 'baseline-home', 'work', 'monitor'):
        cli.add_argument('--' + name, type=Path, required=True)
    args = cli.parse_args()
    if args.work.exists() or [name for _, name in socket.if_nameindex()] != ['lo']:
        cli.error('Requires a fresh work directory and a private loopback-only network')
    args.work.mkdir()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    functions = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-source-function.py'))
    source, chain = functions['source'], functions['chain']
    bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](
        args.bundle, json.loads((ROOT / 'packaging/components.lock.json').read_text()))
    binary, build = source['source_binary'](args.candidates / 'combined', 'combined')
    fixture_meta = json.loads((args.candidates / 'fixture-manifest.json').read_text())
    fixture = args.candidates / 'doh-fixture'
    if (not fixture_meta['fixture_only'] or sha(fixture) != fixture_meta['binary_sha256']
            or sha(ROOT / 'scripts/fixtures/doh/main.go') != fixture_meta['source_sha256']
            or sha(args.baseline_home / 'libexec/mosdns') != json.loads(
                (ROOT / 'packaging/components.lock.json').read_text())['frozen']['mosdns']['sha256']):
        raise ValueError('Fixture or frozen MosDNS differs')
    result = {'state': 'preparing', 'passed': False, 'functional_only': True, 'performance_acceptance': False,
              'release_approved': False, 'clients': 10, 'planned_seconds': 1800, 'stages': [],
              'components': {'bundle_sha256': sha(args.bundle / 'manifest.json'), 'dnscrypt': build,
                             'versions': {k: v['version'] for k, v in bundle['components'].items()},
                             'mosdns_sha256': sha(args.baseline_home / 'libexec/mosdns')},
              'fixture_sha256': sha(fixture), 'method_sha256': sha(__file__)}
    state_path = args.work / 'result.json'
    write(state_path, result)

    def component_command(component, binary):
        entry = bundle['components'][component]
        base = args.bundle / component
        runtime = entry['runtime']
        program = str(base / entry['binaries'][binary])
        if runtime.get('loader'):
            return [str(base / runtime['loader']), '--library-path', str(base / runtime['library_path']), program]
        return [program]

    def guard():
        monitor = json.loads(args.monitor.read_text())
        if (stop.is_set() or not monitor['healthy_to_continue'] or monitor.get('stopped')
                or time.time_ns() - monitor['latest']['unix_ns'] > 35_000_000_000):
            raise RuntimeError('External monitor or termination guard failed')

    try:
        guard()
        with contextlib.ExitStack() as stack:
            cert, key = args.work / 'cert.pem', args.work / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                            '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
            key.chmod(0o600)
            log = args.work / 'doh.log'
            doh = Child([fixture, '-cert', cert, '-key', key, '-protocol', 'h2', '-route', 'direct'], log)
            stack.callback(doh.close)
            end = time.monotonic() + 10
            while not log.stat().st_size:
                doh.check()
                if time.monotonic() > end:
                    raise TimeoutError('Controlled DoH startup timed out')
                time.sleep(.05)
            address = json.loads(log.read_text().splitlines()[0])
            dc_child, dc_port = stack.enter_context(functions['resolver'](
                args.work, binary, cert, int(address['https'].rsplit(':', 1)[1]), None))
            local = Fixture(ttl=600, threaded_udp=False, track_questions=True)
            stack.callback(local.close)
            lab = stack.enter_context(running_lab(
                args.work / 'lab', component_command('redis', 'redis-server'),
                component_command('redis', 'redis-cli'), component_command('unbound', 'unbound'),
                [local, SimpleNamespace(port=dc_port)]))
            # Persist both DB paths and prove the first queries after restart use Redis.
            for port in lab.ports:
                ask(port, 'reload.home.cache.test', 1)
            lab.cli('SAVE')
            lab.restart_all()
            before = chain['redis_counters'](lab)
            for port in lab.ports:
                ask(port, 'reload.home.cache.test', 1)
            after = chain['redis_counters'](lab)
            if {k: after[k] - before[k] for k in before} != {'keyspace_hits': 2, 'keyspace_misses': 0}:
                raise ValueError('Both Redis DB paths were not reloaded')
            result['stages'].append({'stage': 'redis-reload', 'hits': 2, 'misses': 0})
            port = free_port()
            config = chain['mosdns_config'](port, *lab.ports)
            for plugin in config['plugins']:
                if plugin['type'] in ('udp_server', 'tcp_server'):
                    plugin['args']['listen'] = f'[::]:{port}'
            config_path = args.work / 'mosdns.json'
            config_path.write_text(json.dumps(config))
            mosdns = Child(['env', 'GOMAXPROCS=2', args.baseline_home / 'core/mosdns', 'start', '-c', config_path],
                           args.work / 'mosdns.log')
            stack.callback(mosdns.close)
            chain['ready'](mosdns, port)
            children = {'redis': lab.redis, 'unbound0': lab.unbounds[0], 'unbound1': lab.unbounds[1],
                        'dnscrypt': dc_child, 'mosdns': mosdns, 'doh_fixture': doh}
            pids = {name: child.process.pid for name, child in children.items()}
            group = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split(':')[-1].lstrip('/')

            def resources():
                guard()
                rows = {}
                for name, child in children.items():
                    child.check()
                    fields = Path('/proc', str(child.process.pid), 'stat').read_text().rsplit(')', 1)[1].split()
                    rows[name] = {'pid': child.process.pid, 'cpu_ticks': int(fields[11]) + int(fields[12]),
                                  'rss_bytes': int(fields[21]) * os.sysconf('SC_PAGE_SIZE')}
                events = dict(line.split() for line in (group / 'memory.events').read_text().splitlines())
                if any(int(events[k]) for k in ('max', 'oom', 'oom_kill')):
                    raise RuntimeError('Private trial reached its memory limit')
                return {'unix_ns': time.time_ns(), 'processes': rows,
                        'memory_current': int((group / 'memory.current').read_text()),
                        'memory_peak': int((group / 'memory.peak').read_text()), 'memory_events': events,
                        'cpu_stat': dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())}

            for index in range(32):
                name = f'preflight-{index}.{"local" if index & 1 else "enc"}.cache.test'
                ask(port, name, 28 if index & 2 else 1, tcp=bool(index & 4), ipv6=bool(index & 8))
                if index & 16:
                    ask(port, name, 28 if index & 2 else 1, tcp=bool(index & 4), ipv6=bool(index & 8))
            result['stages'].append({'stage': 'protocol-preflight', 'groups': 32, 'queries': 48})
            before = (local.requests(), source['state'](address['state'])['requests'])
            for route in ('local', 'enc'):
                ask(port, f'route-proof.{route}.cache.test', 1)
            after = (local.requests(), source['state'](address['state'])['requests'])
            if [a - b for a, b in zip(after, before)] != [1, 1]:
                raise ValueError('Cold route did not reach the intended upstream')
            for route in ('local', 'enc'):
                ask(port, f'route-proof.{route}.cache.test', 1)
            if after != (local.requests(), source['state'](address['state'])['requests']):
                raise ValueError('Warm cache did not suppress both upstream paths')
            result['stages'].append({'stage': 'split-proof', 'cold_local': 1, 'cold_doh': 1, 'warm_upstream': 0})
            lock = threading.Lock()
            counts = Counter()
            latencies = []
            phase_latency = {'steady': [], 'burst': []}
            began = time.monotonic()
            result.update(state='running', started_unix_ns=time.time_ns(), pids=pids)
            write(state_path, result)
            with (args.work / 'queries.jsonl').open('x', buffering=1) as queries, (
                    args.work / 'resources.jsonl').open('x', buffering=1) as samples:
                def client(identifier):
                    due = began + identifier * .002
                    index = 0
                    while time.monotonic() < began + 1800 and not stop.is_set():
                        time.sleep(max(0, min(due - time.monotonic(), .5)))
                        now = time.monotonic()
                        if now < due:
                            continue
                        if now >= began + 1800:
                            break
                        phase = 'burst' if now >= began + 1790 else 'steady'
                        interval = .1 if phase == 'burst' else 1
                        late = max(0, now - due)
                        due = now + interval
                        pattern = (identifier + index * 10) % 32
                        route = 'local' if pattern & 1 else 'enc'
                        hot = bool(pattern & 16)
                        kind = 28 if pattern & 2 else 1
                        name = (f'hot-{pattern}' if hot else f'cold-{identifier}-{index}') + f'.{route}.cache.test'
                        record = {'client': identifier, 'index': index, 'phase': phase, 'name': name, 'type': kind,
                                  'tcp': bool(pattern & 4), 'ipv6': bool(pattern & 8),
                                  'scheduled_late_ms': late * 1000, 'unix_ns': time.time_ns()}
                        try:
                            elapsed = ask(port, name, kind, tcp=record['tcp'], ipv6=record['ipv6'])
                            record.update(passed=True, elapsed_ms=elapsed)
                        except Exception as error:
                            record.update(passed=False, error=repr(error))
                            stop.set()
                        with lock:
                            queries.write(json.dumps(record) + '\n')
                            counts[phase] += 1
                            counts['passed' if record['passed'] else 'failed'] += 1
                            if record['passed']:
                                latencies.append(record['elapsed_ms'])
                                phase_latency[phase].append(record['elapsed_ms'])
                        index += 1

                with ThreadPoolExecutor(max_workers=10) as pool:
                    tasks = [pool.submit(client, i) for i in range(10)]
                    while not all(task.done() for task in tasks):
                        sample = resources()
                        samples.write(json.dumps(sample) + '\n')
                        with lock:
                            result.update(counts=dict(counts), elapsed_seconds=time.monotonic() - began)
                            write(state_path, result)
                        stop.wait(5)
                    for task in tasks:
                        task.result()
                samples.write(json.dumps(resources()) + '\n')
            if counts['failed'] or counts['steady'] < 16110 or counts['burst'] < 900:
                raise RuntimeError('Controlled query failed or functional workload coverage was insufficient')
            def latency(values):
                ordered = sorted(values)
                return {**{key: ordered[math.ceil(len(ordered) * q) - 1]
                            for key, q in (('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99))},
                        'maximum_ms': max(values), 'above_100ms': sum(v > 100 for v in values),
                        'above_500ms': sum(v > 500 for v in values)}
            http = source['state'](address['state'])
            if http['dns_errors'] or http['rejected'] or http['protocols'] != {'HTTP/2.0': http['requests']}:
                raise ValueError('Controlled encrypted upstream recorded a protocol error')
            result.update(state='complete', passed=True, counts=dict(counts), elapsed_seconds=time.monotonic() - began,
                          latency=latency(latencies), phase_latency={k: latency(v) for k, v in phase_latency.items()},
                          final_redis_counters=chain['redis_counters'](lab),
                          http=http, local_upstream_requests=local.requests())
    except BaseException as error:
        result.update(state='failed', passed=False, error=repr(error))
        if 'counts' in locals():
            result['counts'] = dict(counts)
        raise
    finally:
        stop.set()
        result['finished_unix_ns'] = time.time_ns()
        write(state_path, result)


if __name__ == '__main__':
    main()
