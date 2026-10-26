"""DNS wire responses and loopback transports; no public resolver required."""
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as backend

AUTH = {"Authorization": "Bearer test-token"}


def answer(name, kind, value, ttl=60, record_class=1):
    data = (socket.inet_pton(socket.AF_INET, value) if kind == 1
            else socket.inet_pton(socket.AF_INET6, value) if kind == 28
            else backend._encode_dns_name(value))
    return (backend._encode_dns_name(name) + struct.pack("!HHIH", kind, record_class, ttl, len(data))
            + data)


def response(query, records=(), flags=0x8180):
    query_id = struct.unpack("!H", query[:2])[0]
    return struct.pack("!HHHHHH", query_id, flags, 1, len(records), 0, 0) + query[12:] + b"".join(records)


@pytest.mark.parametrize("records,expected", [
    ([], "no_records"),
    ([answer("example.com", 5, "target.example.com")], "no_records"),
    ([answer("other.example.com", 1, "192.0.2.1")], "no_records"),
    ([answer("example.com", 1, "192.0.2.1", record_class=3)], "no_records"),
    ([answer("example.com", 5, "target.example.com"),
      answer("target.example.com", 1, "192.0.2.1", ttl=0)], "ok"),
])
def test_lookup_requires_address_on_cname_chain(monkeypatch, records, expected):
    monkeypatch.setattr(backend, "_exchange_dns", lambda query, *args: response(query, records))
    result = backend._dns_lookup("example.com", "A", "127.0.0.1", 53)
    assert result["status"] == expected
    assert result["available"] == (expected == "ok")
    assert result["responded"] is True
    if expected == "ok":
        assert result["results"] == ["192.0.2.1"]
        assert result["answers"][-1]["ttl"] == 0


@pytest.mark.parametrize("record_type,records", [
    ("AAAA", [answer("example.com", 28, "2001:db8::1")]),
    ("CNAME", [answer("example.com", 5, "target.example.com")]),
])
def test_requested_record_types(monkeypatch, record_type, records):
    monkeypatch.setattr(backend, "_exchange_dns", lambda query, *args: response(query, records))
    assert backend._dns_lookup("example.com", record_type, "127.0.0.1", 53)["available"] is True


@pytest.mark.parametrize("flags,status", [(0x8183, "dns_error"), (0x8182, "dns_error"), (0x8380, "incomplete")])
def test_error_rcode_and_tcp_truncation_do_not_pass(monkeypatch, flags, status):
    monkeypatch.setattr(backend, "_exchange_dns", lambda query, *args: response(query, flags=flags))
    result = backend._dns_lookup("example.com", "A", "127.0.0.1", 53, "tcp")
    assert result["status"] == status
    assert result["available"] is False


def test_partial_udp_record_triggers_tcp_and_reports_failure(monkeypatch):
    calls = []

    def exchange(query, server, port, tcp=False):
        calls.append(tcp)
        if tcp:
            raise TimeoutError("TCP timed out")
        # TC bit set with an answer cut short inside RDATA.
        return response(query, [answer("example.com", 1, "192.0.2.1")], 0x8380)[:-2]

    monkeypatch.setattr(backend, "_exchange_dns", exchange)
    result = backend._dns_lookup("example.com", "A", "127.0.0.1", 53)
    assert calls == [False, True]
    assert result["available"] is False
    assert result["status"] == "incomplete"
    assert result["transport"] == "tcp"
    assert result["tcp_fallback"] is True
    assert result["answers"] == []


@pytest.mark.parametrize("kind", ["question", "id", "qr", "address_length"])
def test_mismatched_or_malformed_response_is_rejected(monkeypatch, kind):
    def exchange(query, *args):
        packet = response(query, [answer("example.com", 1, "192.0.2.1")])
        if kind == "question":
            return packet[:13] + b"z" + packet[14:]
        if kind == "id":
            return bytes([packet[0] ^ 1]) + packet[1:]
        if kind == "qr":
            return packet[:2] + b"\x01\x80" + packet[4:]
        # Four RDATA bytes declared as three.
        return packet[:-6] + b"\x00\x03" + packet[-4:]

    monkeypatch.setattr(backend, "_exchange_dns", exchange)
    result = backend._dns_lookup("example.com", "A", "127.0.0.1", 53)
    assert result["status"] == "invalid_response"
    assert result["available"] is False


def test_health_check_rejects_cname_without_target(monkeypatch):
    def exchange(query, *args):
        name, _ = backend._read_dns_name(query, 12)
        return response(query, [answer(name, 5, "target.example.com")])

    monkeypatch.setattr(backend, "_exchange_dns", exchange)
    result = backend._run_health_check("127.0.0.1", 53)
    assert result["pass"] is False
    assert all(item["result"]["status"] == "no_records" for item in result["tests"].values())


@pytest.fixture
def local_resolver():
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("127.0.0.1", 0))
    port = tcp.getsockname()[1]
    tcp.listen(1)
    tcp.settimeout(3)
    failures = []
    threads = []

    def start():
        def serve_tcp():
            try:
                connection, _ = tcp.accept()
                with connection:
                    deadline = backend.time.monotonic() + 3
                    size = struct.unpack("!H", backend._recv_exact(connection, 2, deadline))[0]
                    query = backend._recv_exact(connection, size, deadline)
                    packet = response(query, [answer("example.com", 1, "192.0.2.9", ttl=0)])
                    framed = struct.pack("!H", len(packet)) + packet
                    for byte in framed:
                        connection.sendall(bytes([byte]))
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=serve_tcp, daemon=True)
        thread.start()
        threads.append(thread)
        return port

    yield start
    for thread in threads:
        thread.join(4)
        assert not thread.is_alive()
    tcp.close()
    assert not failures


@pytest.mark.parametrize("transport", ["udp", "tcp"])
def test_loopback_tcp_and_udp_fallback(local_resolver, monkeypatch, transport):
    port = local_resolver()
    exchange = backend._exchange_dns

    def truncated_udp(query, server, port, tcp=False):
        # Simulate the truncated datagram, then use real loopback TCP for fallback.
        if not tcp:
            return response(query, [answer("example.com", 1, "192.0.2.1")], 0x8380)[:-2]
        return exchange(query, server, port, True)

    if transport == "udp":
        monkeypatch.setattr(backend, "_exchange_dns", truncated_udp)
    result = backend._dns_lookup("example.com", "A", "127.0.0.1", port, transport)
    assert result["available"] is True, result
    assert result["results"] == ["192.0.2.9"]
    assert result["transport"] == "tcp"
    assert result["tcp_fallback"] == (transport == "udp")


def test_udp_transport_connects_to_selected_peer(monkeypatch):
    class Datagram:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def settimeout(self, timeout):
            assert 0 < timeout <= backend.DNS_TEST_TIMEOUT
        def connect(self, address):
            assert address == ("192.0.2.53", 5353)
        def send(self, data):
            assert data == b"query"
        def recv(self, size):
            return b"response"

    monkeypatch.setattr(backend.socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.53", 5353))])
    monkeypatch.setattr(backend.socket, "socket", lambda *args: Datagram())
    assert backend._exchange_dns(b"query", "dns.local", 5353) == b"response"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(backend, "WEB_UI_TOKEN", "test-token")
    monkeypatch.setattr(backend, "WEB_UI_ALLOW_NO_AUTH", False)
    with backend.app.test_client() as client:
        yield client


def test_compare_requires_auth(client):
    assert client.post("/api/dns-compare", json={}).status_code == 401


def test_dns_test_empty_optional_targets_keep_configured_defaults(client, monkeypatch):
    calls = []

    def fake_lookup(domain, record_type, server, port):
        calls.append((domain, record_type, server, port))
        return {"available": False, "domain": domain, "record_type": record_type,
                "server": server, "port": port, "answers": [], "results": [],
                "status": "timeout", "transport": "udp"}

    monkeypatch.setattr(backend, "DNS_TEST_SERVER", "dns.default")
    monkeypatch.setattr(backend, "DNS_TEST_PORT", 5353)
    monkeypatch.setattr(backend, "_dns_lookup", fake_lookup)
    response = client.post("/api/dns-test", headers=AUTH,
                           json={"domain": "example.com", "server": "", "port": ""})
    assert response.status_code == 200
    assert calls == [("example.com", "A", "dns.default", 5353)]


def test_compare_keeps_failed_endpoint_and_target_order(client, monkeypatch):
    def exchange(query, server, port, tcp=False):
        if port == 5301:
            raise TimeoutError("unreachable")
        return response(query, [answer("example.com", 1, "192.0.2.1")])

    monkeypatch.setattr(backend, "_exchange_dns", exchange)
    result = client.post("/api/dns-compare", headers=AUTH, json={
        "domain": "example.com", "record_type": "A", "targets": [
            {"label": "recursive", "server": "127.0.0.1", "port": 5301},
            {"label": "frontend", "server": "127.0.0.1", "port": 53},
        ],
    })
    assert result.status_code == 200
    data = result.get_json()
    assert data["kind"] == "paopaodns-dns-report"
    assert [test["label"] for test in data["tests"]] == ["recursive", "frontend"]
    assert [test["result"]["status"] for test in data["tests"]] == ["timeout", "ok"]


@pytest.mark.parametrize("payload", [[], {"targets": []}, {"domain": "example.com", "targets": [{}] * 5},
    {"domain": "example.com", "targets": [{"label": "test", "server": 123}]},
    {"domain": "example.com", "targets": [{"label": "test", "port": 1.5}]},
    {"domain": "example.com", "targets": [{"label": "test", "transport": []}]},
    {"domain": "example.com", "record_type": {}, "targets": [{"label": "test"}]},
])
def test_compare_invalid_input_never_sends_queries(client, monkeypatch, payload):
    def unexpected(*args):
        pytest.fail("Invalid input must not send a DNS query")

    monkeypatch.setattr(backend, "_exchange_dns", unexpected)
    assert client.post("/api/dns-compare", headers=AUTH, json=payload).status_code == 400
