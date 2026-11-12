from collections import Counter
import http.client
import http.server
from pathlib import Path
import runpy
import socket
import struct
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dns_lab import exact, question

module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'test-dnscrypt-compatibility.py'))


def test_previous_case_connection_cannot_certify_a_new_socks_case():
    with pytest.raises(AssertionError, match='new SOCKS'):
        module['route_evidence'](True, Counter(), Counter(proxy=1), 1, 1)


@pytest.mark.parametrize('tcp_nodelay', [False, True])
def test_proxy_fixture_rejects_direct_requests_and_accepts_real_socks_forwarding(tcp_nodelay):
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), module['DoH'])
    httpd.fixture = SimpleNamespace(respond=lambda packet, tcp: packet)
    httpd.routes, httpd.route_lock, httpd.proxy_only = Counter(), threading.Lock(), True
    socks = module['Socks'](('127.0.0.1', 0), module['SocksHandler'])
    socks.allowed_port, socks.connections = httpd.server_port, 0
    socks.tcp_nodelay = tcp_nodelay
    for server in (httpd, socks):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    packet = question('route.cache.test')
    try:
        direct = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=2)
        try:
            direct.request('POST', '/dns-query', body=packet)
            response = direct.getresponse()
            assert response.status == 403
            response.read()
        finally:
            direct.close()
        before = httpd.routes.copy()
        with socket.create_connection(socks.server_address, timeout=2) as client:
            client.sendall(b'\x05\x01\x00')
            assert exact(client, 2) == b'\x05\x00'
            client.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + struct.pack('!H', httpd.server_port))
            assert exact(client, 10)[1] == 0
            client.sendall(f'POST /dns-query HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(packet)}\r\nConnection: close\r\n\r\n'.encode() + packet)
            response = http.client.HTTPResponse(client)
            response.begin()
            assert response.status == 200
            assert response.read() == packet
        evidence = module['route_evidence'](True, before, httpd.routes, 0, socks.connections)
        assert evidence['doh_routes'] == {'direct': 0, 'proxy': 1, 'rejected': 0}
    finally:
        for server in (socks, httpd):
            server.shutdown()
            server.server_close()
