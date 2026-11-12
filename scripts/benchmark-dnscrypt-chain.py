"""Bounded normal-binary DNSCrypt and frozen MosDNS split-path comparisons."""
import argparse
from collections import Counter
import contextlib
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import socket
import subprocess
import threading
import time

from dns_lab import CacheLab, Child, Fixture, free_port, query

ROOT = Path(__file__).resolve().parents[1]
source = runpy.run_path(str(ROOT / 'scripts/benchmark-dnscrypt-source.py'))
compat = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-compatibility.py'))
common = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))
report = runpy.run_path(str(ROOT / 'scripts/report-dns-benchmark.py'))
COMPONENTS = ('dnscrypt', 'mosdns', 'unbound', 'redis')
SCENARIOS = ('dnscrypt-cold', 'dnscrypt-hot', 'split-cold', 'split-hot')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def question_rows(scenario, count):
    if scenario not in SCENARIOS or count < 1:
        raise ValueError('Unknown chain scenario or empty workload')
    rows = []
    for index in range(count):
        local = scenario.startswith('split-') and index % 2 == 0
        suffix = 'local.cache.test' if local else 'enc.cache.test'
        key = index % 16 if scenario.endswith('-hot') else index
        rows.append((f'q-{key}.{suffix}', 'AAAA' if index % 5 == 0 else 'A'))
    return rows


def expected_queries(scenario, rows):
    if scenario.endswith('-hot'):
        return Counter(), 0
    local = Counter((name, 28 if kind == 'AAAA' else 1, 1) for name, kind in rows
                    if scenario.startswith('split-') and name.endswith('.local.cache.test'))
    return local, len(rows) - sum(local.values())


def path_evidence(scenario, rows, local_before, local_after, before, after, redis_before, redis_after, protocol):
    expected_local, encrypted = expected_queries(scenario, rows)
    actual_local = Counter()
    for (name, kind, cls, transport), count in (local_after - local_before).items():
        actual_local[(name, kind, cls)] += count
    http = source['protocol_evidence'](before, after, protocol)
    if encrypted == 0:
        http['valid'] = (http['requests'] == 0 and not any(http[key] for key in ('routes', 'protocols', 'alpn'))
                         and http['rejected'] == http['dns_errors'] == 0)
    redis_delta = {name: redis_after[name] - redis_before[name] for name in redis_before}
    checks = {'local_questions': actual_local == expected_local,
              'encrypted_requests': http['requests'] == encrypted,
              'protocol': http['valid'],
              'upper_cache_blocks_backends': (not scenario.endswith('-hot') or
                  not actual_local and http['requests'] == 0 and all(value == 0 for value in redis_delta.values()))}
    return {'checks': checks, 'valid': all(checks.values()), 'local_requests': sum(actual_local.values()),
            'expected_local_requests': sum(expected_local.values()), 'expected_encrypted_requests': encrypted,
            'protocol': http, 'redis_delta': redis_delta,
            'local_questions': [[*key, count] for key, count in sorted(actual_local.items())]}


def mosdns_config(port, local_port, encrypted_port):
    return {'log': {'level': 'error'}, 'plugins': [
        {'tag': 'local_names', 'type': 'domain_set', 'args': {'exps': ['domain:local.cache.test']}},
        {'tag': 'local_backend', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{local_port}'}]}},
        {'tag': 'encrypted_backend', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{encrypted_port}'}]}},
        {'tag': 'accept', 'type': 'sequence', 'args': [{'exec': 'ok'}]},
        {'tag': 'local_sequence', 'type': 'sequence', 'args': [{'exec': '$local_backend'}, {'exec': 'goto accept'}]},
        {'tag': 'main', 'type': 'sequence', 'args': [
            {'exec': 'cache 4096'}, {'matches': 'has_wanted_ans', 'exec': 'goto accept'},
            {'matches': 'qname $local_names', 'exec': 'goto local_sequence'}, {'exec': '$encrypted_backend'}]},
        *[{'tag': kind, 'type': kind, 'args': {'entry': 'main', 'listen': f'127.0.0.1:{port}'}}
          for kind in ('udp_server', 'tcp_server')]]}


def runtime_identity(home):
    home = Path(home).resolve()
    names = [*(home / 'core' / name for name in ('mosdns', 'unbound', 'redis-server', 'redis-cli')),
             *(home / 'libexec' / name for name in ('mosdns', 'unbound', 'redis-server', 'redis-cli')),
             *(path for path in (home / 'lib').iterdir() if path.is_file())]
    return {path.relative_to(home).as_posix(): digest(path) for path in sorted(names)}


def redis_counters(lab):
    return common['redis_counters'](lab)


def ready(child, port):
    deadline = time.monotonic() + 15
    while True:
        child.check()
        try:
            if query(port, 'ready.enc.cache.test', timeout=.3)['answers']:
                return
        except (OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError('Controlled chain readiness deadline')
        time.sleep(.05)


def checks(row, environment, path):
    # The generic report verifies load/counts/capture, not this split topology.
    base = report['check_run'](row, {**environment, 'entry': 'unbound'})
    resources = row.get('resources', {})
    base['all_components'] = all(report['number'](resources.get(name, {}).get(field))
        for name in COMPONENTS for field in ('cpu_seconds', 'peak_rss_bytes'))
    base['split_path'] = path['valid']
    base['pacing'] = source['pacing_evidence'](row, environment['qps'])['valid']
    base['wire_timing'] = row['answer_validation'].get('wire_latency', {}).get('complete') is True
    sender = row.get('generator_pacing', {})
    base['sender'] = (sender.get('method') == 'monotonic-deadlines-no-late-catchup-v1'
                      and sender.get('outstanding_limit_skips') == sender.get('malformed_or_unexpected') == 0)
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'candidates', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--duration', type=int, default=5)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--cases', default='baseline,upstream,cache')
    parser.add_argument('--scenarios', default=','.join(SCENARIOS))
    parser.add_argument('--routes', default='direct,socks')
    args = parser.parse_args()
    cases, scenarios, routes = (getattr(args, name).split(',') for name in ('cases', 'scenarios', 'routes'))
    for values, allowed in ((cases, {'baseline', 'upstream', 'cache', 'combined'}), (scenarios, set(SCENARIOS)), (routes, {'direct', 'socks'})):
        if not values or len(values) != len(set(values)) or set(values) - allowed:
            parser.error('Select distinct supported cases, scenarios and routes')
    if args.work.exists() or not 5 <= args.duration <= 120 or not 1 <= args.repeats <= 5:
        parser.error('Use a fresh directory and bounded duration/repeats')
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Requires a disposable network namespace with loopback enabled')
    home, candidates, work = (getattr(args, name).resolve() for name in ('baseline_home', 'candidates', 'work'))
    identity = runtime_identity(home)
    builds, binaries = {}, {}
    for label in cases:
        binaries[label], builds[label] = (source['baseline_binary'](home) if label == 'baseline' else
                                          source['source_binary'](candidates / label, label))
    helpers = {}
    for binary, filename, relative in (('doh-fixture', 'fixture-manifest.json', 'doh'),
                                       ('paced-udp', 'generator-manifest.json', 'udp-load')):
        manifest = json.loads((candidates / filename).read_text())
        if (manifest.get('fixture_only') is not True or manifest['binary_sha256'] != digest(candidates / binary)
                or manifest['source_sha256'] != digest(ROOT / 'scripts/fixtures' / relative / 'main.go')):
            raise ValueError('Helper differs from its source/build manifest')
        helpers[binary] = manifest
    work.mkdir()
    cert, key = work / 'cert.pem', work / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    key.chmod(0o600)
    environment = {'entry': 'controlled-split-chain', 'qps': 100, 'max_outstanding': 64,
        'duration': args.duration, 'repeats': args.repeats, 'cases': cases, 'scenarios': scenarios, 'routes': routes,
        'protocol': 'h2', 'affinity': sorted(os.sched_getaffinity(0)), 'runtime_sha256': identity,
        'builds': builds, 'helpers': helpers, 'method_sha256': digest(Path(__file__)), 'release_approved': False,
        'limitations': ['Controlled domain split and cache/forward topology, not production geo/rule/fallback coverage.',
                        'Unbound, Redis and customized MosDNS remain the same baseline binaries in every case.',
                        'Fixed offered load; not maximum throughput; short or invalid rounds are not performance acceptance.']}
    (work / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    outcomes = []
    for route in routes:
        proxy = route == 'socks'
        fixture_log = work / f'doh-{route}.log'
        fixture = Child([candidates / 'doh-fixture', '-cert', cert, '-key', key, '-protocol', 'h2',
                         '-route', 'proxy' if proxy else 'direct'], fixture_log)
        socks = None
        try:
            deadline = time.monotonic() + 10
            while not fixture_log.stat().st_size:
                fixture.check()
                if time.monotonic() > deadline:
                    raise TimeoutError('DoH readiness deadline')
                time.sleep(.05)
            address = json.loads(fixture_log.read_text().splitlines()[0])
            https_port = int(address['https'].rsplit(':', 1)[1])
            socks = compat['Socks'](('127.0.0.1', 0), compat['SocksHandler'])
            socks.allowed_port, socks.connections, socks.tcp_nodelay = https_port, 0, True
            threading.Thread(target=socks.serve_forever, daemon=True).start()
            for scenario in scenarios:
                for repeat in range(args.repeats):
                    for label in cases[repeat % len(cases):] + cases[:repeat % len(cases)]:
                        case = work / f'{route}-{scenario}-{repeat}-{label}'
                        case.mkdir()
                        with contextlib.ExitStack() as stack:
                            local = Fixture(ttl=600, threaded_udp=False, track_questions=True)
                            stack.callback(local.close)
                            lab = CacheLab(case / 'local', [home / 'core/redis-server'], [home / 'core/redis-cli'],
                                           [home / 'core/unbound'], [local])
                            stack.callback(lab.close)
                            port = free_port()
                            config = case / 'dnscrypt.toml'
                            config.write_text(f'''server_names = ['fixture']
listen_addresses = ['127.0.0.1:{port}']
max_clients = 250
ipv4_servers = true
ipv6_servers = false
dnscrypt_servers = false
doh_servers = true
require_dnssec = false
require_nolog = false
require_nofilter = false
http3 = false
ignore_system_dns = true
netprobe_address = '127.0.0.1:{https_port}'
netprobe_timeout = 3
cache = true
cache_size = 512
cache_min_ttl = 0
cache_max_ttl = 600
''' + (f"proxy = 'socks5://127.0.0.1:{socks.server_address[1]}'\n" if proxy else '') +
                                ("pqdnscrypt = false\nlb_strategy = 'p2'\n" if label != 'baseline' else '') +
                                f"[static.'fixture']\nstamp = '{compat['stamp'](https_port)}'\n")
                            initial, connections = source['state'](address['state']), socks.connections
                            dnscrypt = Child(['setpriv', '--bounding-set=-net_admin', 'env', 'GOMAXPROCS=2',
                                'SSL_CERT_FILE=' + str(cert), binaries[label], '-config', config], case / 'dnscrypt.log')
                            stack.callback(dnscrypt.close)
                            ready(dnscrypt, port)
                            mos_port = free_port()
                            mos_config = case / 'mosdns.json'
                            mos_config.write_text(json.dumps(mosdns_config(mos_port, lab.ports[0], port)))
                            mosdns = Child(['env', 'GOMAXPROCS=2', home / 'core/mosdns', 'start', '-c', mos_config], case / 'mosdns.log')
                            stack.callback(mosdns.close)
                            ready(mosdns, mos_port)
                            entrance = mos_port if scenario.startswith('split-') else port
                            rows = question_rows(scenario, args.duration * 100 + 10)
                            if scenario.endswith('-hot'):
                                for name, kind in sorted(set(rows)):
                                    response = query(entrance, name, qtype=28 if kind == 'AAAA' else 1)
                                    if response['rcode'] != 0 or not response['answers']:
                                        raise ValueError('Cache prewarm failed')
                            warm = source['state'](address['state'])
                            warm_route = compat['route_evidence'](proxy, Counter(initial['routes']), Counter(warm['routes']),
                                                                  connections, socks.connections)
                            warm_protocol = source['protocol_evidence'](initial, warm, 'h2')
                            if not warm_protocol['valid']:
                                raise ValueError('No observed HTTP/2/TLS warmup evidence')
                            before, local_before, redis_before = warm, local.question_snapshot(), redis_counters(lab)
                            data = case / 'questions.txt'
                            data.write_text(''.join(f'{name} {kind}\n' for name, kind in rows))
                            processes = {'dnscrypt': dnscrypt.process.pid, 'mosdns': mosdns.process.pid,
                                'unbound': lab.unbounds[0].process.pid, 'redis': lab.redis.process.pid,
                                'fixture': fixture.process.pid, 'python_fixture': os.getpid()}
                            row = common['load'](candidates / 'paced-udp', entrance, data, args.duration, 100,
                                case / 'load.txt', processes, {1: '192.0.2.10', 28: '2001:db8::10'}, max_outstanding=64)
                            after = source['state'](address['state'])
                            path = path_evidence(scenario, rows[:row['sent']], local_before, local.question_snapshot(),
                                before, after, redis_before, redis_counters(lab), 'h2')
                            with gzip.open(case / 'load.txt.gz', 'rt') as stream:
                                match = re.search(r'^Paced UDP evidence: (.+)$', stream.read(), re.M)
                            row.update(case=label, scenario=scenario, route=route, repeat=repeat, protocol='h2',
                                path_validation=path, warm_route=warm_route, warm_protocol=warm_protocol,
                                generator_pacing=json.loads(match[1]) if match else {},
                                upstream_requests=after['requests'] - before['requests'])
                            row['measurement_checks'] = checks(row, environment, path)
                            row['component_cpu_seconds'] = sum(row['resources'][name]['cpu_seconds'] for name in COMPONENTS)
                            row['component_peak_rss_sum_bytes'] = sum(row['resources'][name]['peak_rss_bytes'] for name in COMPONENTS)
                            outcomes.append(row)
                            (work / 'results.json').write_text(json.dumps({'environment': environment, 'runs': outcomes,
                                'release_approved': False}, indent=2) + '\n')
                            print(json.dumps({name: row[name] for name in ('case', 'scenario', 'route', 'repeat', 'completed',
                                'component_cpu_seconds', 'measurement_checks')}), flush=True)
                            for child in (dnscrypt, mosdns, lab.redis, *lab.unbounds):
                                child.check()
                            if not all(row['measurement_checks'].values()):
                                raise RuntimeError('Chain measurement conditions failed; retain evidence and stop this round')
        finally:
            if socks:
                socks.shutdown()
                socks.server_close()
            fixture.close()
    if runtime_identity(home) != identity:
        raise ValueError('Baseline runtime identity changed')


if __name__ == '__main__':
    main()
