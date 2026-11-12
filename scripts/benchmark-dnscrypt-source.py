"""Screen normal DNSCrypt source candidates with observed TLS/HTTP and UDP pacing."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import threading
import time
import urllib.request

from dns_lab import Child, free_port, query

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/dnscrypt-source.lock.json'
PACING_METHOD = 'kernel-arrival-paced-100qps-v1'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_binary(home, expected_variant=None):
    manifest = json.loads((home / 'source-manifest.json').read_text())
    lock = json.loads(LOCK.read_text())
    variant = manifest.get('variant')
    if expected_variant is not None and variant != expected_variant:
        raise ValueError('Source candidate is filed under the wrong comparison case')
    if (manifest.get('candidate_only') is not True or manifest.get('diagnostic_only') is not False
            or manifest.get('release_approved') is not False or variant not in lock['variants']):
        raise ValueError('Requires an explicit normal source candidate')
    expected = {name: lock['patches'][name] for name in lock['variants'][variant]}
    if (manifest.get('source_lock_sha256') != digest(LOCK) or manifest.get('patches_sha256') != expected
            or manifest.get('source') != lock['source'] or manifest.get('toolchain') != lock['toolchain']):
        raise ValueError('Candidate differs from the reviewed source lock')
    binary = (home / 'dnscrypt-proxy').resolve()
    if digest(binary) != manifest.get('binary_sha256'):
        raise ValueError('Source candidate binary digest mismatch')
    return binary, manifest


def baseline_binary(home):
    home = home.resolve()
    wrapper, binary = home / 'core/dnscrypt-proxy', home / 'libexec/dnscrypt-proxy'
    files = [wrapper, binary, *(p for p in (home / 'lib').iterdir() if p.is_file())]
    return wrapper, {'deployed_baseline': True, 'binary_sha256': digest(binary),
                     'runtime_sha256': {p.relative_to(home).as_posix(): digest(p) for p in sorted(files)}}


def pacing_evidence(row, qps):
    """Reject scheduling gaps and catch-up bursts instead of averaging them away."""
    timing = row.get('answer_validation', {}).get('arrival_timing', {})
    stream = timing.get('streams', {}).get('query', {})
    period = 1_000_000_000 / qps
    checks = {
        'kernel_timestamps': timing.get('method') == 'linux-kernel-so-timestampns-v1',
        'all_queries_seen': stream.get('count') == row.get('sent') and row.get('sent', 0) > 1,
        'ordered': stream.get('out_of_order') == 0,
        'no_catchup': isinstance(stream.get('min_gap_ns'), int) and stream['min_gap_ns'] >= period / 2,
        'no_long_gap': isinstance(stream.get('max_gap_ns'), int) and stream['max_gap_ns'] <= period * 5,
        'bounded_1ms_bins': stream.get('peak_packets_per_1ms') == 1,
    }
    return {'method': PACING_METHOD, 'expected_period_ns': period,
            'min_gap_ns': stream.get('min_gap_ns'), 'max_gap_ns': stream.get('max_gap_ns'),
            'checks': checks, 'valid': all(checks.values())}


def protocol_evidence(before, after, protocol):
    keys = ('routes', 'protocols', 'alpn')
    result = {key: {name: count - before.get(key, {}).get(name, 0)
                    for name, count in after[key].items()
                    if count != before.get(key, {}).get(name, 0)} for key in keys}
    result.update({key: after[key] - before[key] for key in ('requests', 'connections', 'rejected', 'dns_errors')})
    expected_proto, expected_alpn = ('HTTP/2.0', 'h2') if protocol == 'h2' else ('HTTP/1.1', 'http/1.1')
    count = result['requests']
    result['valid'] = (count > 0 and result['protocols'] == {expected_proto: count}
                       and result['alpn'] == {expected_alpn: count}
                       and result['rejected'] == result['dns_errors'] == 0)
    return result


def state(address):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://' + address + '/state', timeout=3) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'candidates', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--protocols', default='h1,h2')
    parser.add_argument('--cases', default='baseline,upstream,udp')
    parser.add_argument('--scenarios', default='doh-direct,doh-socks')
    parser.add_argument('--load-generator', choices=('strict-v1', 'dnsperf'), default='strict-v1')
    parser.add_argument('--dnsperf', type=Path, help='Required only with --load-generator dnsperf')
    args = parser.parse_args()
    cases, protocols, scenarios = args.cases.split(','), args.protocols.split(','), args.scenarios.split(',')
    if (args.work.exists() or not 5 <= args.duration <= 120 or not 1 <= args.repeats <= 5
            or not cases or len(set(cases)) != len(cases) or set(cases) - {'baseline', 'upstream', 'buffer', 'udp', 'cache', 'combined'}
            or not protocols or len(set(protocols)) != len(protocols) or set(protocols) - {'h1', 'h2'}
            or not scenarios or len(set(scenarios)) != len(scenarios) or set(scenarios) - {'doh-direct', 'doh-socks'}):
        parser.error('Use a new work directory, bounded limits and distinct supported cases/protocols/scenarios')
    if args.load_generator == 'dnsperf' and args.dnsperf is None:
        parser.error('dnsperf mode requires --dnsperf')
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Requires a disposable network namespace with loopback enabled')
    compat = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-compatibility.py'))
    load = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))['load']
    report = runpy.run_path(str(ROOT / 'scripts/report-dns-benchmark.py'))
    receive_buffer = runpy.run_path(str(ROOT / 'scripts/profile-dnscrypt.py'))['receive_buffer']
    fixture_binary = (args.candidates / 'doh-fixture').resolve()
    fixture_manifest = json.loads((args.candidates / 'fixture-manifest.json').read_text())
    if (fixture_manifest.get('fixture_only') is not True or digest(fixture_binary) != fixture_manifest['binary_sha256']
            or digest(ROOT / 'scripts/fixtures/doh/main.go') != fixture_manifest['source_sha256']):
        raise ValueError('DoH fixture differs from its build manifest or the current method')
    generator_manifest = None
    if args.load_generator == 'strict-v1':
        generator = (args.candidates / 'paced-udp').resolve()
        generator_manifest = json.loads((args.candidates / 'generator-manifest.json').read_text())
        if (generator_manifest.get('fixture_only') is not True or digest(generator) != generator_manifest['binary_sha256']
                or digest(ROOT / 'scripts/fixtures/udp-load/main.go') != generator_manifest['source_sha256']):
            raise ValueError('Paced generator differs from its build manifest or the current method')
    else:
        generator = args.dnsperf.resolve()
    commands, builds = {}, {}
    for label in cases:
        if label == 'baseline':
            binary, manifest = baseline_binary(args.baseline_home)
            commands[label], builds[label] = [str(binary)], manifest
        else:
            binary, manifest = source_binary(args.candidates / label, label)
            commands[label], builds[label] = [str(binary)], manifest
    work = args.work.resolve()
    work.mkdir(parents=True)
    cert, key = (work / name for name in ('cert.pem', 'key.pem'))
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                    '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    key.chmod(0o600)
    environment = {'entry': 'dnscrypt', 'duration': args.duration, 'repeats': args.repeats,
                   'qps': 100, 'max_outstanding': 64, 'cases': cases, 'scenarios': scenarios,
                   'protocols': protocols, 'pacing_method': PACING_METHOD, 'socks_tcp_nodelay': True,
                   'resolver_net_admin': False, 'resolver_gomaxprocs': 2, 'affinity': sorted(os.sched_getaffinity(0)),
                   'builds': builds, 'fixture': fixture_manifest,
                   'load_generator': args.load_generator, 'generator': generator_manifest,
                   'method_sha256': {name: digest(ROOT / 'scripts' / name) for name in
                       ('benchmark-dnscrypt-source.py', 'benchmark-dns.py', 'test-dnscrypt-compatibility.py',
                        'dns_capture.py', 'dns_lab.py', 'report-dns-benchmark.py')},
                   'limitations': ['New Go TLS fixture, NODELAY and per-packet pacing checks require a new baseline.',
                                   'Fixture TCP connection counts include DNSCrypt startup netprobes.',
                                   'Controlled 100 QPS A/AAAA misses; not production splitting or maximum throughput.',
                                   'Source candidates are not approved for default installation.']}
    outcomes = []
    (work / 'environment.json').write_text(json.dumps(environment, indent=2) + '\n')
    for protocol in protocols:
        for scenario in scenarios:
            proxy = scenario == 'doh-socks'
            fixture_log = work / (protocol + '-' + scenario + '-fixture.log')
            fixture = Child([str(fixture_binary), '-cert', str(cert), '-key', str(key),
                             '-protocol', protocol, '-route', 'proxy' if proxy else 'direct'], fixture_log)
            socks = None
            try:
                deadline = time.monotonic() + 10
                while not fixture_log.stat().st_size:
                    fixture.check()
                    if time.monotonic() > deadline:
                        raise TimeoutError('DoH fixture readiness deadline')
                    time.sleep(.05)
                address = json.loads(fixture_log.read_text().splitlines()[0])
                https_port = int(address['https'].rsplit(':', 1)[1])
                socks = compat['Socks'](('127.0.0.1', 0), compat['SocksHandler'])
                socks.allowed_port, socks.connections, socks.tcp_nodelay = https_port, 0, True
                threading.Thread(target=socks.serve_forever, daemon=True).start()
                for repeat in range(args.repeats):
                    # Rotate three cases instead of always placing the middle one second.
                    order = cases[repeat % len(cases):] + cases[:repeat % len(cases)]
                    for label in order:
                        case = work / f'{protocol}-{scenario}-{repeat}-{label}'
                        case.mkdir()
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
                        initial, socks_before = state(address['state']), socks.connections
                        child = Child(['setpriv', '--bounding-set=-net_admin', 'env', 'SSL_CERT_FILE=' + str(cert),
                                       'GOMAXPROCS=2',
                                       *commands[label], '-config', str(config)], case / 'process.log')
                        try:
                            deadline = time.monotonic() + 15
                            while True:
                                child.check()
                                try:
                                    if query(port, 'ready.cache.test', timeout=.3)['answers']:
                                        break
                                except (OSError, ValueError):
                                    pass
                                if time.monotonic() > deadline:
                                    raise TimeoutError('DNSCrypt readiness deadline')
                                time.sleep(.1)
                            rb = receive_buffer(port)
                            before = state(address['state'])
                            data = case / 'questions.txt'
                            data.write_text('\n'.join(f'cold-{i}.cache.test ' + ('AAAA' if i % 5 == 0 else 'A')
                                                      for i in range(100 * args.duration + 10)) + '\n')
                            row = load(generator, port, data, args.duration, 100, case / 'load.txt',
                                       {'dnscrypt': child.process.pid, 'fixture': fixture.process.pid,
                                        'socks_fixture': os.getpid()}, {1: '192.0.2.10', 28: '2001:db8::10'}, max_outstanding=64)
                            after = state(address['state'])
                            route = compat['route_evidence'](proxy, Counter(initial['routes']), Counter(after['routes']),
                                                             socks_before, socks.connections)
                            protocol_check = protocol_evidence(before, after, protocol)
                            row.update(scenario=scenario, protocol=protocol, case=label, repeat=repeat,
                                       upstream_requests=after['requests'] - before['requests'], tls_verified=True,
                                       route_validation=route, protocol_validation=protocol_check,
                                       fixture_tcp_connections=after['connections'] - initial['connections'],
                                       socket_receive_buffer_bytes=rb)
                            row['pacing_validation'] = pacing_evidence(row, 100)
                            if args.load_generator == 'strict-v1':
                                with gzip.open(case / 'load.txt.gz', 'rt') as stream:
                                    evidence = re.search(r'^Paced UDP evidence: (.+)$', stream.read(), re.M)
                                row['generator_pacing'] = json.loads(evidence[1]) if evidence else {}
                            sender = row.get('generator_pacing', {})
                            row['measurement_checks'] = {**report['check_run'](row, environment),
                                                         'protocol': protocol_check['valid'],
                                                         'wire_timing': row['answer_validation'].get('wire_latency', {}).get('complete') is True,
                                                         'sender': (args.load_generator == 'dnsperf' or
                                                                    sender.get('method') == 'monotonic-deadlines-no-late-catchup-v1'
                                                                    and sender.get('outstanding_limit_skips') == 0
                                                                    and sender.get('malformed_or_unexpected') == 0),
                                                         'pacing': row['pacing_validation']['valid']}
                            outcomes.append(row)
                            (work / 'results.json').write_text(json.dumps({'environment': environment, 'runs': outcomes}, indent=2) + '\n')
                            print(json.dumps({name: row[name] for name in
                                ('protocol', 'scenario', 'case', 'repeat', 'completed', 'p95_ms', 'p99_ms', 'measurement_checks')}), flush=True)
                            child.check()
                            if not all(row['measurement_checks'].values()):
                                raise RuntimeError('Measurement checks failed; retain this run and diagnose before continuing')
                        finally:
                            child.close()
            finally:
                if socks:
                    socks.shutdown()
                    socks.server_close()
                fixture.close()


if __name__ == '__main__':
    main()
