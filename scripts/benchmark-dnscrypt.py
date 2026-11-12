"""Measure positive DNS forwarding through disposable TLS DoH and SOCKS fixtures."""
import argparse
from collections import Counter
import hashlib
import http.server
import json
import os
from pathlib import Path
import runpy
import ssl
import subprocess
import threading
import time

from dns_lab import Child, Fixture, candidate_command, free_port, query

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'bundle', 'dnsperf', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--qps', type=int, default=100)
    parser.add_argument('--socks-tcp-nodelay', action='store_true',
                        help='Diagnostic fixture variable; changes the protocol baseline')
    args = parser.parse_args()
    if args.work.exists() or min(args.duration, args.repeats, args.qps) < 1:
        parser.error('Use positive limits and a new disposable directory')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    bundle = runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    compat = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-compatibility.py'))
    load = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))['load']
    args.work.mkdir(parents=True)
    cert, key = [(args.work / n).resolve() for n in ('cert.pem', 'key.pem')]
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    key.chmod(0o600)
    fixture = Fixture(ttl=600, threaded_udp=False)
    class Handler(compat['DoH']):
        disable_nagle_algorithm = True
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    httpd.fixture, httpd.routes, httpd.route_lock = fixture, Counter(), threading.Lock()
    httpd.proxy_only = False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    socks = compat['Socks'](('127.0.0.1', 0), compat['SocksHandler'])
    socks.allowed_port, socks.connections = httpd.server_port, 0
    socks.tcp_nodelay = args.socks_tcp_nodelay
    threading.Thread(target=socks.serve_forever, daemon=True).start()
    environment = {'entry': 'dnscrypt', 'duration': args.duration, 'repeats': args.repeats, 'qps': args.qps,
                   'max_outstanding': 64, 'socks_tcp_nodelay': args.socks_tcp_nodelay,
                   'cases': ['baseline', 'dnscrypt-only'],
                   'scenarios': ['doh-direct', 'doh-socks'], 'affinity': sorted(os.sched_getaffinity(0)),
                   'lock_sha256': bundle['lock_sha256'], 'tool_sha256': hashlib.sha256(args.dnsperf.read_bytes()).hexdigest(),
                   'method_sha256': {n: hashlib.sha256((ROOT / 'scripts' / n).read_bytes()).hexdigest()
                                      for n in ('benchmark-dnscrypt.py', 'benchmark-dns.py', 'test-dnscrypt-compatibility.py',
                                                'dns_capture.py', 'dns_lab.py', 'dns_seed.py', 'report-dns-benchmark.py')},
                   'limitations': ['Controlled HTTP/1.1 TLS fixture; HTTP/2, HTTP/3 and DNSCrypt protocol are not measured.',
                                   'Direct DNSCrypt entrance; not production splitting, full recursion, or maximum throughput.',
                                   'The Python fixture resource row includes DoH and SOCKS together.']}
    results = []
    try:
        for scenario in environment['scenarios']:
            proxy = scenario == 'doh-socks'
            httpd.proxy_only = proxy
            for repeat in range(args.repeats):
                order = environment['cases'] if repeat % 2 == 0 else list(reversed(environment['cases']))
                for label in order:
                    work = args.work / f'{scenario}-{repeat}-{label}'; work.mkdir()
                    port = free_port(); config = work.resolve() / 'dnscrypt.toml'
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
netprobe_address = '127.0.0.1:{httpd.server_port}'
netprobe_timeout = 3
cache = true
cache_size = 512
cache_min_ttl = 0
cache_max_ttl = 600
''' + (f"proxy = 'socks5://127.0.0.1:{socks.server_address[1]}'\n" if proxy else '') +
                        ("pqdnscrypt = false\nlb_strategy = 'p2'\n" if label != 'baseline' else '') +
                        f"[static.'fixture']\nstamp = '{compat['stamp'](httpd.server_port)}'\n")
                    command = ([str((args.baseline_home / 'core/dnscrypt-proxy').resolve())] if label == 'baseline'
                               else candidate_command(args.bundle, 'dnscrypt', 'dnscrypt-proxy'))
                    connections_before = socks.connections
                    with httpd.route_lock: routes_before = httpd.routes.copy()
                    child = Child(['env', 'SSL_CERT_FILE=' + str(cert), *command, '-config', str(config)], work / 'process.log')
                    try:
                        deadline = time.monotonic() + 15
                        while True:
                            child.check()
                            try:
                                if query(port, 'ready.cache.test', timeout=.3)['answers']: break
                            except (OSError, ValueError): pass
                            if time.monotonic() > deadline: raise TimeoutError('DNSCrypt fixture readiness timed out')
                            time.sleep(.1)
                        data = work / 'questions.txt'
                        data.write_text('\n'.join(f'cold-{i}.cache.test ' + ('AAAA' if i % 5 == 0 else 'A')
                                                  for i in range(args.qps * args.duration + 10)) + '\n')
                        upstream_before = fixture.requests()
                        row = load(args.dnsperf.resolve(), port, data.resolve(), args.duration, args.qps, work / 'dnsperf.txt',
                                   {'dnscrypt': child.process.pid, 'fixture': os.getpid()},
                                   {1: fixture.address, 28: '2001:db8::10'}, max_outstanding=64)
                        with httpd.route_lock:
                            evidence = compat['route_evidence'](proxy, routes_before, httpd.routes, connections_before, socks.connections)
                        row.update(scenario=scenario, case=label, repeat=repeat, upstream_requests=fixture.requests() - upstream_before,
                                   route_validation=evidence, tls_verified=True)
                        results.append(row)
                        (args.work / 'results.json').write_text(json.dumps({'environment': environment, 'runs': results}, indent=2) + '\n')
                        print(json.dumps({k: row[k] for k in ('scenario', 'case', 'repeat', 'qps', 'p95_ms', 'p99_ms', 'lost')}), flush=True)
                        assert row['lost'] == 0 and row['rcodes'] == {'NOERROR': row['completed']}, row
                        assert row['answer_validation']['complete'] and row['answer_validation']['errors'] == 0, row
                    finally: child.close()
    finally:
        socks.shutdown(); socks.server_close(); httpd.shutdown(); httpd.server_close(); fixture.close()


if __name__ == '__main__': main()
