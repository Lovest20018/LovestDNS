"""Exercise old/new DNSCrypt cache and DoH, directly and through local SOCKS5."""
import argparse
import base64
from collections import Counter
import http.server
import json
import os
import runpy
from pathlib import Path
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse

from dns_lab import Child, Fixture, candidate_command, exact, free_port, query

PROXY_SOURCE = '127.0.0.2'


class DoH(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def answer(self, packet):
        source = self.client_address[0]
        route = 'proxy' if source == PROXY_SOURCE else 'direct'
        with self.server.route_lock:
            self.server.routes[route] += 1
            allowed = source == (PROXY_SOURCE if self.server.proxy_only else '127.0.0.1')
            if not allowed:
                self.server.routes['rejected'] += 1
        if not allowed:
            self.send_error(403, 'Unexpected route to the disposable DoH fixture')
            return
        response = self.server.fixture.respond(packet, True)
        self.send_response(200)
        self.send_header('Content-Type', 'application/dns-message')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        value = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)['dns'][0]
        self.answer(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))

    def do_POST(self):
        self.answer(self.rfile.read(min(65535, int(self.headers['Content-Length']))))


def route_evidence(proxy, before, after, connections_before, connections_after):
    routes = {name: after[name] - before[name] for name in ('direct', 'proxy', 'rejected')}
    connections = connections_after - connections_before
    expected, unexpected = ('proxy', 'direct') if proxy else ('direct', 'proxy')
    assert routes[expected] > 0 and routes[unexpected] == 0 and routes['rejected'] == 0, routes
    assert (connections > 0 if proxy else connections == 0), 'No new SOCKS connection for this case'
    return {'doh_routes': routes, 'socks_connections': connections}


class Socks(socketserver.ThreadingTCPServer):
    daemon_threads = True


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        # Opt-in diagnostic variable; keep historical measurements unchanged.
        if getattr(self.server, 'tcp_nodelay', False):
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            version, count = exact(self.request, 2)
            assert version == 5 and 0 in exact(self.request, count)
            self.request.sendall(b'\x05\x00')
            version, command, _, kind = exact(self.request, 4)
            assert version == 5 and command == 1
            if kind == 1:
                address = socket.inet_ntoa(exact(self.request, 4))
            elif kind == 3:
                address = exact(self.request, exact(self.request, 1)[0]).decode()
            else:
                raise ValueError('Unsupported test proxy target')
            port = struct.unpack('!H', exact(self.request, 2))[0]
            if address not in {'127.0.0.1', 'localhost'} or port != self.server.allowed_port:
                raise ValueError('Proxy fixture only permits its local DoH endpoint')
            with socket.create_connection(('127.0.0.1', port), timeout=5, source_address=(PROXY_SOURCE, 0)) as peer:
                if getattr(self.server, 'tcp_nodelay', False):
                    peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.server.connections += 1
                self.request.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                while True:
                    readable, _, _ = select.select([self.request, peer], [], [], 5)
                    if not readable:
                        break
                    for current in readable:
                        data = current.recv(65535)
                        if not data:
                            return
                        (peer if current is self.request else self.request).sendall(data)
        except (OSError, ValueError, AssertionError):
            pass


def stamp(port):
    def field(text):
        value = text.encode()
        return bytes([len(value)]) + value
    value = b'\x02' + b'\0' * 8 + field('127.0.0.1') + b'\0' + field('localhost:' + str(port)) + field('/dns-query')
    return 'sdns://' + base64.urlsafe_b64encode(value).decode().rstrip('=')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-home', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    if args.work.exists():
        parser.error('Use a new disposable work directory')
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(root / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    args.work.mkdir(parents=True)
    cert, key = (args.work.resolve() / name for name in ('fixture-cert.pem', 'fixture-key.pem'))
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    key.chmod(0o600)
    fixture = Fixture()

    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), DoH)
    httpd.fixture, httpd.routes, httpd.route_lock = fixture, Counter(), threading.Lock()
    httpd.proxy_only = False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    socks = Socks(('127.0.0.1', 0), SocksHandler)
    socks.allowed_port, socks.connections = httpd.server_port, 0
    threading.Thread(target=socks.serve_forever, daemon=True).start()
    outcomes = []
    try:
        for label, command in [('baseline', [str((args.baseline_home / 'core/dnscrypt-proxy').resolve())]),
                               ('candidate', candidate_command(args.bundle, 'dnscrypt', 'dnscrypt-proxy'))]:
            for proxy in (False, True):
                httpd.proxy_only = proxy
                connections_before = socks.connections
                with httpd.route_lock:
                    routes_before = httpd.routes.copy()
                port = free_port()
                work = args.work / (label + ('-socks' if proxy else '-direct'))
                work.mkdir()
                config = work.resolve() / 'dnscrypt.toml'
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
                                  f"[static.'fixture']\nstamp = '{stamp(httpd.server_port)}'\n")
                if label == 'candidate':
                    config.write_text('pqdnscrypt = false\nlb_strategy = "p2"\n' + config.read_text())
                # Go's system certificate pool includes only this fixture's test
                # CA in this child; no host trust store or TLS policy is changed.
                argv = ['env', 'SSL_CERT_FILE=' + str(cert), *command, '-config', str(config)]
                child = Child(argv, work / 'process.log')
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        child.check()
                        assert httpd.routes['rejected'] == routes_before['rejected'], 'DNSCrypt bypassed the required DoH route'
                        try:
                            result = query(port, 'ready.cache.test', timeout=.3)
                            if result['rcode'] == 0 and result['answers']:
                                break
                        except (OSError, ValueError):
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError((work / 'process.log').read_text()[-3000:])
                        time.sleep(.1)
                    name = label + ('-proxy' if proxy else '-direct') + '.cache.test'
                    result = query(port, name)
                    assert result['rcode'] == 0 and result['answers'][0]['value'] == fixture.address, result
                    before = fixture.requests()
                    assert query(port, name)['answers'][0]['value'] == fixture.address
                    assert fixture.requests() == before, 'Repeated answer missed DNSCrypt cache'
                    assert query(port, name, qtype=28)['answers'][0]['value'] == '2001:db8::10'
                    assert query(port, 'missing.' + name)['rcode'] == 3
                    alias = query(port, 'cname.' + name)['answers']
                    assert [item['value'] for item in alias] == ['target.cache.test', fixture.address], alias
                    with httpd.route_lock:
                        evidence = route_evidence(proxy, routes_before, httpd.routes, connections_before, socks.connections)
                    outcome = {'case': label, 'socks': proxy, 'doh_tls_verified': True, 'cache_hit': True,
                               'ipv6_answer': True, 'negative_answer': True, **evidence}
                    outcomes.append(outcome)
                    print(json.dumps(outcome), flush=True)
                finally:
                    child.close()
        (args.work / 'results.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    finally:
        socks.shutdown(); socks.server_close()
        httpd.shutdown(); httpd.server_close()
        fixture.close()


if __name__ == '__main__':
    main()
