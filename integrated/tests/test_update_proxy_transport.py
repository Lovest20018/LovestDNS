"""Real socket/TLS proxy exchanges without reaching public DNS or GitHub."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import shutil
import hashlib
import importlib.util
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import threading
import urllib.error
import urllib.request

import pytest
from runtime import software_update as protocol
from runtime import update_network as network


def certificate(tmp_path):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    openssl = shutil.which('openssl')
    if openssl:
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=github.com',
            '-addext', 'subjectAltName=DNS:github.com,DNS:api.github.com,DNS:release-assets.githubusercontent.com'],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # Windows developers may lack an openssl executable. CI uses OpenSSL;
        # the optional local fallback does not add a production dependency.
        pytest.importorskip('cryptography')
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'github.com')])
        now = datetime.now(timezone.utc)
        value = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(private.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host) for host in (
                'github.com', 'api.github.com', 'release-assets.githubusercontent.com')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(private, hashes.SHA256()))
        cert.write_bytes(value.public_bytes(serialization.Encoding.PEM))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert, key


def headers(sock):
    result = bytearray()
    while not result.endswith(b'\r\n\r\n'):
        result.extend(network.receive(sock, 1))
        if len(result) > 16384:
            raise OSError('Oversize test headers')
    return bytes(result)


@contextmanager
def proxy_fixture(tmp_path, mode, authenticated=True, reject=False):
    cert, key = certificate(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    seen = {'destinations': [], 'requests': [], 'sni': []}
    context.set_servername_callback(lambda sock, name, _: seen['sni'].append(name))
    user, password = b'fixture-user', b'synthetic-secret'

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            sock.settimeout(4)
            try:
                if mode == 'http':
                    value = headers(sock)
                    seen['destinations'].append(value.split(b'\r\n')[0])
                    expected = b'Proxy-Authorization: Basic ' + base64.b64encode(user + b':' + password)
                    if reject or (authenticated and expected not in value):
                        sock.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                        return
                    sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                else:
                    assert network.receive(sock, 3) == bytes((5, 1, 2 if authenticated else 0))
                    sock.sendall(bytes((5, 2 if authenticated else 0)))
                    if authenticated:
                        assert network.receive(sock, 1) == b'\x01'
                        actual_user = network.receive(sock, network.receive(sock, 1)[0])
                        actual_password = network.receive(sock, network.receive(sock, 1)[0])
                        accepted = not reject and actual_user == user and actual_password == password
                        sock.sendall(bytes((1, 0 if accepted else 1)))
                        if not accepted:
                            return
                    assert network.receive(sock, 4) == b'\x05\x01\x00\x03'
                    host = network.receive(sock, network.receive(sock, 1)[0]).decode()
                    port = struct.unpack('!H', network.receive(sock, 2))[0]
                    seen['destinations'].append((host, port))
                    sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb')
                with context.wrap_socket(sock, server_side=True) as secure:
                    request = headers(secure)
                    seen['requests'].append(request)
                    if request.split(b' ')[1] == b'/start':
                        response = b'HTTP/1.1 302 Found\r\nLocation: https://release-assets.githubusercontent.com/end\r\nContent-Length: 0\r\n\r\n'
                    elif request.split(b' ')[1] == b'/unsafe':
                        response = b'HTTP/1.1 302 Found\r\nLocation: https://foreign.example/end\r\nContent-Length: 0\r\n\r\n'
                    elif request.split(b' ')[1].endswith(b'.sha256'):
                        payload = (hashlib.sha256(b'fixture').hexdigest() + '  ' + protocol.ASSET + '\n').encode()
                        response = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload
                    else:
                        response = b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture'
                    secure.sendall(response)
            except ssl.SSLError:
                pass  # Expected when the client rejects this test certificate.

    class Proxy(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with Proxy(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        settings = {'mode': mode, 'host': '127.0.0.1', 'port': server.server_address[1],
                    'username': user.decode() if authenticated else '',
                    'password': password.decode() if authenticated else ''}
        try:
            yield settings, cert, seen
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize('mode', ['http', 'socks5'])
@pytest.mark.parametrize('authenticated', [False, True])
def test_https_and_github_redirect_use_same_proxy_without_local_destination_dns(tmp_path, monkeypatch, mode, authenticated):
    with proxy_fixture(tmp_path, mode, authenticated) as (settings, cert, seen):
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
        original = socket.getaddrinfo
        def resolve(host, *args, **kwargs):
            assert host == '127.0.0.1', 'Destination DNS escaped the proxy'
            return original(host, *args, **kwargs)
        monkeypatch.setattr(socket, 'getaddrinfo', resolve)
        monkeypatch.setenv('https_proxy', 'http://127.0.0.1:1')
        monkeypatch.setenv('no_proxy', '*')
        assert protocol.public_bytes('https://github.com/start', 20, opener=network.opener(settings)) == b'fixture'
        assert seen['sni'] == ['github.com', 'release-assets.githubusercontent.com']
        assert len(seen['destinations']) == 2
        assert all(b'Proxy-Authorization' not in request and b'synthetic-secret' not in request for request in seen['requests'])
        if mode == 'socks5':
            assert seen['destinations'] == [('github.com', 443), ('release-assets.githubusercontent.com', 443)]


@pytest.mark.parametrize('mode', ['http', 'socks5'])
def test_proxy_tls_is_verified_and_untrusted_certificate_is_rejected(tmp_path, mode):
    with proxy_fixture(tmp_path, mode) as (settings, _, seen):
        with pytest.raises(protocol.UpdateError, match='无法从 GitHub'):
            protocol.public_bytes('https://github.com/end', 20, opener=network.opener(settings))
        assert not seen['requests']


@pytest.mark.parametrize('mode', ['http', 'socks5'])
def test_rejected_authentication_fails_without_direct_fallback(tmp_path, monkeypatch, mode):
    with proxy_fixture(tmp_path, mode, reject=True) as (settings, _, seen):
        original = socket.create_connection
        def connect(address, *args, **kwargs):
            assert address == (settings['host'], settings['port']), 'Unexpected direct fallback'
            return original(address, *args, **kwargs)
        monkeypatch.setattr(socket, 'create_connection', connect)
        with pytest.raises(protocol.UpdateError):
            protocol.public_bytes('https://github.com/end', 20, opener=network.opener(settings))
        assert not seen['requests']


def test_proxy_does_not_bypass_redirect_allowlist_or_size_limit(tmp_path, monkeypatch):
    with proxy_fixture(tmp_path, 'socks5') as (settings, cert, seen):
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
        transport = network.opener(settings)
        with pytest.raises(protocol.UpdateError, match='重定向'):
            protocol.public_bytes('https://github.com/unsafe', 20, opener=transport)
        with pytest.raises(protocol.UpdateError, match='大小限制'):
            protocol.public_bytes('https://github.com/end', 3, opener=transport)
        assert len(seen['destinations']) == 2


def test_direct_mode_ignores_host_proxy_environment(monkeypatch):
    monkeypatch.setenv('https_proxy', 'http://127.0.0.1:1')
    transport = network.opener(network.DEFAULT)
    assert not any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies for handler in transport.handlers)


def test_production_installer_uses_proxy_for_archive_and_checksum(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('proxy_release_download', Path(__file__).resolve().parents[2] / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    with proxy_fixture(tmp_path, 'socks5') as (settings, cert, seen):
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
        destination = tmp_path / 'download'
        destination.mkdir()
        archive, digest = installer.download('test.next', destination, opener=network.opener(settings))
        assert archive.read_bytes() == b'fixture'
        assert digest == hashlib.sha256(b'fixture').hexdigest()
        assert seen['destinations'] == [('github.com', 443), ('github.com', 443)]
        assert all(b'Proxy-Authorization' not in request for request in seen['requests'])
