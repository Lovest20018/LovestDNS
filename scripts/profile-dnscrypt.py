"""Locate DNSCrypt hotspots with a disposable HTTP/1.1 TLS fixture in a network namespace."""
import argparse
from collections import Counter
import concurrent.futures
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import runpy
import ssl
import subprocess
import threading
import time
import urllib.request

from dns_lab import Child, Fixture, free_port, query

ROOT = Path(__file__).resolve().parents[1]


def diagnostic_binary(home):
    manifest = json.loads((home / 'diagnostic-manifest.json').read_text())
    binary = (home / 'dnscrypt-diagnostic').resolve()
    if manifest.get('diagnostic_only') is not True or manifest.get('deployable') is not False:
        raise ValueError('Requires a diagnostic build, not a service binary')
    if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest['binary_sha256']:
        raise ValueError('Diagnostic binary digest mismatch')
    return binary, manifest


def fetch(port, path, output, timeout):
    # Ignore host proxy variables: profiles never leave the private loopback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/debug/pprof/{path}', timeout=timeout) as response:
        output.write_bytes(response.read())


def receive_buffer(port):
    lines = subprocess.check_output(['ss', '-u', '-n', '-a', '-m'], text=True, timeout=5).splitlines()
    for index, line in enumerate(lines[:-1]):
        if re.search(rf'127\.0\.0\.1:{port}\s', line):
            match = re.search(r'\brb(\d+)', lines[index + 1])
            if match:
                return int(match[1])
    raise RuntimeError('Resolver receive buffer could not be measured')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('diagnostic-home', 'dnsperf', 'pprof-go', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mode', choices=('cpu', 'heap'), required=True)
    parser.add_argument('--scenario', choices=('direct', 'socks'), default='direct')
    parser.add_argument('--duration', type=int, default=45)
    parser.add_argument('--qps', type=int, default=100)
    parser.add_argument('--default-udp-buffer', action='store_true',
                        help='Drop NET_ADMIN from the diagnostic child so the upstream FORCE call cannot shrink the socket buffer')
    args = parser.parse_args()
    if args.work.exists() or not 5 <= args.duration <= 120 or not 1 <= args.qps <= 500:
        parser.error('Use a new work directory, duration 5..120s and QPS 1..500')
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Run inside a disposable network namespace with loopback enabled')
    binary, manifest = diagnostic_binary(args.diagnostic_home)
    compat = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-compatibility.py'))
    load = runpy.run_path(str(ROOT / 'scripts/benchmark-dns.py'))['load']
    work = args.work.resolve()
    work.mkdir(parents=True)
    cert, key = (work / name for name in ('cert.pem', 'key.pem'))
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                    '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    key.chmod(0o600)
    fixture = Fixture(ttl=600, threaded_udp=False)

    class Handler(compat['DoH']):
        disable_nagle_algorithm = True

        def answer(self, packet):
            with self.server.route_lock:
                self.server.connections.add(self.client_address)
                self.server.protocols[(self.request_version, self.connection.selected_alpn_protocol())] += 1
            super().answer(packet)

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    httpd.fixture, httpd.routes, httpd.route_lock = fixture, Counter(), threading.Lock()
    httpd.connections, httpd.protocols = set(), Counter()
    proxy = args.scenario == 'socks'
    httpd.proxy_only = proxy
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.set_alpn_protocols(['http/1.1'])
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    socks = compat['Socks'](('127.0.0.1', 0), compat['SocksHandler'])
    socks.allowed_port, socks.connections = httpd.server_port, 0
    for server in (httpd, socks):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    child = None
    try:
        port, profile_port = free_port(), free_port()
        config = work / 'dnscrypt.toml'
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
            ("pqdnscrypt = false\nlb_strategy = 'p2'\n" if manifest['version'] == '2.1.20' else '') +
            f"[static.'fixture']\nstamp = '{compat['stamp'](httpd.server_port)}'\n")
        command = (['setpriv', '--bounding-set=-net_admin'] if args.default_udp_buffer else [])
        child = Child([*command, 'env', 'SSL_CERT_FILE=' + str(cert),
                       f'LOVESTDNS_DIAGNOSTICS_ADDR=127.0.0.1:{profile_port}',
                       'LOVESTDNS_DIAGNOSTICS_MODE=' + args.mode,
                       str(binary), '-config', str(config)], work / 'process.log')
        deadline = time.monotonic() + 15
        while True:
            child.check()
            try:
                if query(port, 'ready.cache.test', timeout=.3)['answers']:
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError('Diagnostic resolver readiness timed out')
            time.sleep(.1)
        data = work / 'questions.txt'
        socket_receive_buffer = receive_buffer(port)
        duration = args.duration + 2 if args.mode == 'cpu' else args.duration
        data.write_text('\n'.join(f'cold-{i}.cache.test ' + ('AAAA' if i % 5 == 0 else 'A')
                                  for i in range(args.qps * duration + 10)) + '\n')
        if args.mode == 'heap':
            fetch(profile_port, 'heap?gc=1', work / 'before.pprof', 10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            if args.mode == 'cpu':
                future = pool.submit(fetch, profile_port, f'profile?seconds={args.duration}',
                                     work / 'cpu.pprof', args.duration + 15)
                time.sleep(.25)
            before = fixture.requests()
            result = load(args.dnsperf.resolve(), port, data, duration, args.qps, work / 'dnsperf.txt',
                          {'dnscrypt': child.process.pid, 'fixture': os.getpid()},
                          {1: fixture.address, 28: '2001:db8::10'}, max_outstanding=64)
            if future:
                future.result(timeout=args.duration + 15)
        if args.mode == 'heap':
            fetch(profile_port, 'heap?gc=1', work / 'after.pprof', 10)
        with httpd.route_lock:
            route = compat['route_evidence'](proxy, Counter(), httpd.routes, 0, socks.connections)
            connections = len(httpd.connections)
            protocols = {str(key): value for key, value in httpd.protocols.items()}
        result.update(diagnostic_only=True, release_acceptance=False, mode=args.mode,
                      diagnostic_build=manifest, scenario=args.scenario, qps_requested=args.qps,
                      net_admin_dropped=args.default_udp_buffer,
                      socket_receive_buffer_bytes=socket_receive_buffer,
                      upstream_requests=fixture.requests() - before, route_validation=route,
                      doh_connections=connections, observed_http_protocols=protocols,
                      method_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                     (Path(__file__), ROOT / 'scripts/test-dnscrypt-compatibility.py')})
        (work / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
        assert result['lost'] == 0 and result['rcodes'] == {'NOERROR': result['completed']}, result
        assert result['answer_validation']['complete'] and result['answer_validation']['errors'] == 0, result
        assert abs(result['completed'] - args.qps * duration) <= args.qps, 'Incomplete diagnostic offered load'
    finally:
        if child:
            child.close()
        for server in (socks, httpd):
            server.shutdown()
            server.server_close()
        fixture.close()
    samples = [('cpu', 'cpu.pprof')] if args.mode == 'cpu' else [
        ('alloc_space', 'after.pprof'), ('alloc_objects', 'after.pprof'), ('inuse_space', 'after.pprof')]
    for sample, filename in samples:
        for cumulative in (False, True):
            command = [str(args.pprof_go.resolve()), 'tool', 'pprof', '-top', '-nodecount=30', '-sample_index=' + sample]
            if cumulative:
                command.append('-cum')
            if sample.startswith('alloc_'):
                command += ['-base', str(work / 'before.pprof')]
            command += [str(binary), str(work / filename)]
            output = work / (sample + ('-cum' if cumulative else '') + '.txt')
            with output.open('w') as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=30)
    print(json.dumps({'version': manifest['version'], 'mode': args.mode, 'completed': result['completed'],
                      'doh_connections': connections, 'work': str(work)}), flush=True)


if __name__ == '__main__':
    main()
