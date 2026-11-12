"""Validate client diagnostics using actual wire packets, with no external DNS."""

import argparse
import json
from pathlib import Path
import runpy
import socket
import struct
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
client = runpy.run_path(str(SCRIPTS / 'check-home-dns.py'))


def encoded(name):
    return b''.join(bytes([len(part)]) + part.encode() for part in name.split('.')) + b'\0'


def answer(query, *, owner='target.example.com', alias=True, flags=0x8180):
    records = b''
    if alias:
        target = encoded('target.example.com')
        records += b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 0, len(target)) + target
    records += encoded(owner) + struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton('192.0.2.10')
    return struct.pack('!HHHHHH', int.from_bytes(query[:2], 'big'), flags, 1, 2 if alias else 1, 0, 0) + query[12:] + records


def test_cname_and_zero_ttl_are_valid_but_unrelated_answers_are_not(monkeypatch):
    run = client['probe']
    for owner, alias, expected in [('target.example.com', True, 'ok'), ('other.example.com', False, 'no_records')]:
        monkeypatch.setitem(run.__globals__, 'exchange', lambda packet, *_, owner=owner, alias=alias:
                            answer(packet, owner=owner, alias=alias))
        row = run('192.0.2.1', 53, 'example.com', 'A', 'udp', 1)
        assert row['status'] == expected
        assert row['available'] == (expected == 'ok')
        if row['available']:
            assert row['records'][0]['ttl'] == 0


@pytest.mark.parametrize('change', ['transaction', 'question', 'qtype', 'response_flag', 'truncated_name'])
def test_malformed_or_mismatched_packets_never_report_success(monkeypatch, change):
    run = client['probe']

    def exchange(query, *_):
        packet = bytearray(answer(query))
        if change == 'transaction': packet[0] ^= 1
        if change == 'question': packet[13] ^= 1
        if change == 'qtype': packet[len(query) - 3] = 28
        if change == 'response_flag': packet[2] &= 0x7f
        if change == 'truncated_name': packet = packet[:12] + b'\xc0'
        return bytes(packet)

    monkeypatch.setitem(run.__globals__, 'exchange', exchange)
    row = run('192.0.2.1', 53, 'example.com', 'A', 'udp', 1)
    assert row['status'] == 'invalid_response' and not row['available']


def test_truncated_udp_requires_a_successful_tcp_fallback(monkeypatch):
    run = client['probe']
    calls = []

    def exchange(packet, server, port, tcp, timeout):
        calls.append(tcp)
        if tcp: raise TimeoutError('TCP timeout')
        return struct.pack('!HHHHHH', int.from_bytes(packet[:2], 'big'), 0x8380, 1, 0, 0, 0) + packet[12:]

    monkeypatch.setitem(run.__globals__, 'exchange', exchange)
    row = run('192.0.2.1', 53, 'example.com', 'A', 'udp', 1)
    assert calls == [False, True]
    assert row['responded'] and row['tcp_fallback'] and not row['available'] and row['status'] == 'timeout'


def test_tcp_read_uses_one_deadline_instead_of_resetting_for_each_fragment(monkeypatch):
    read = client['receive']
    times = iter([1, 1.5, 3])
    monkeypatch.setattr(client['time'], 'monotonic', lambda: next(times))

    class Stream:
        def settimeout(self, value): pass
        def recv(self, size): return b'x'

    with pytest.raises(TimeoutError): read(Stream(), 3, 2)


@pytest.mark.parametrize('name', ['bad..example', '-bad.example', 'x' * 64 + '.example'])
def test_invalid_domain_is_an_input_error(name):
    with pytest.raises(argparse.ArgumentTypeError): client['domain'](name)


def test_interrupted_round_retains_partial_evidence_without_passing(monkeypatch, tmp_path):
    main = client['main']
    target = tmp_path / 'partial.json'
    monkeypatch.setattr(sys, 'argv', ['check-home-dns.py', '--server', '192.0.2.1',
                                    '--domain', 'example.com', '--output', str(target)])
    calls = 0

    def probe(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return {'status': 'ok', 'available': True}

    monkeypatch.setitem(main.__globals__, 'probe', probe)
    with pytest.raises(KeyboardInterrupt): main()
    result = json.loads(target.read_text(encoding='utf-8'))
    assert result['state'] == 'running' and not result['passed']
    assert result['expected_queries'] == 4 and len(result['queries']) == 1
