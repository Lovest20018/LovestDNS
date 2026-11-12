import ipaddress
import json
import queue
import sqlite3
import struct
import time

import pytest
from runtime.query_wire import Correlator, dns_message, name_at, MAX_PENDING, WAIT_SECONDS
from runtime.query_store import Store
from runtime.query_recorder import Recorder, validate_request
from runtime.query_capture import filter_program
from test_integrated_api import client, AUTH
import server


def dns(reply=False, domain='example.test', qtype=1, ident=10, rcode=0):
    question = b''.join(bytes([len(p)]) + p.encode() for p in domain.split('.')) + b'\0' + struct.pack('!HH', qtype, 1)
    answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 30, 4) + bytes([192, 0, 2, 8])
    return struct.pack('!6H', ident, 0x8180 | rcode if reply else 0x100, 1, int(reply and not rcode), 0, 0) + question + (answer if reply and not rcode else b'')


def packet(payload, reply=False, tcp=False, seq=1, flags=0x18, ipv6=False, port=15453):
    source, target = ('2001:db8::2', '2001:db8::1') if ipv6 else ('192.0.2.2', '192.0.2.1')
    sport, dport = 32123, port
    if reply:
        source, target, sport, dport = target, source, dport, sport
    if tcp:
        transport = struct.pack('!HHIIBBHHH', sport, dport, seq, 0, 0x50, flags, 4096, 0, 0) + payload
    else:
        transport = struct.pack('!HHHH', sport, dport, len(payload) + 8, 0) + payload
    src, dst = ipaddress.ip_address(source).packed, ipaddress.ip_address(target).packed
    if ipv6:
        return struct.pack('!IHBB', 6 << 28, len(transport), 6 if tcp else 17, 64) + src + dst + transport
    return struct.pack('!BBHHHBBH', 0x45, 0, 20 + len(transport), 1, 0, 64, 6 if tcp else 17, 0) + src + dst + transport


def correlator(transport='udp', **kwargs):
    events = []
    endpoints = [{'component': 'mosdns', 'address': '::', 'port': 15453, 'transport': transport}]
    return Correlator(endpoints, events.append, **kwargs), events


@pytest.mark.parametrize('ipv6', [False, True])
def test_real_wire_pair_has_client_transport_rcode_addresses_and_interval(ipv6):
    recorder, events = correlator()
    recorder.feed(packet(dns(), ipv6=ipv6), 100, 5)
    recorder.feed(packet(dns(True), True, ipv6=ipv6), 100.125, 5.125)
    assert len(events) == 1
    event = events[0]
    assert event['source_ip'] == ('2001:db8::2' if ipv6 else '192.0.2.2')
    assert event['elapsed_ms'] == 125 and event['rcode'] == 'NOERROR'
    assert event['addresses'] == ['192.0.2.8']
    assert event['cache_hit'] is None and event['branch'] is None
    assert event['timing_scope'] == 'passive_entry_packet_interval'


def test_negative_no_response_same_key_and_unmatched_are_distinct():
    recorder, events = correlator()
    recorder.feed(packet(dns()), 100, 5)
    recorder.feed(packet(dns()), 100.1, 5.1)
    recorder.feed(packet(dns(True, rcode=3), True), 100.2, 5.2)
    assert events[0]['rcode'] == 'NXDOMAIN' and events[0]['same_key_packets'] == 2
    recorder.feed(packet(dns(ident=20)), 101, 6)
    recorder.expire(6 + WAIT_SECONDS)
    assert events[1]['outcome'] == 'no_response_observed'
    assert events[1]['elapsed_ms'] is None and events[1]['rcode'] is None
    recorder.feed(packet(dns(True, ident=99), True), 102, 7)
    assert recorder.counters['unmatched_responses'] == 1


def test_tcp_requires_handshake_and_reassembles_split_length_prefix_and_payload():
    recorder, events = correlator('tcp')
    query, reply = dns(), dns(True)
    recorder.feed(packet(struct.pack('!H', len(query)) + query, tcp=True), 99, 1)
    assert recorder.counters['tcp_untracked_packets'] == 1
    recorder.feed(packet(b'', tcp=True, seq=100, flags=2), 100, 2)
    recorder.feed(packet(b'', True, True, seq=200, flags=0x12), 100, 2)
    frame = struct.pack('!H', len(query)) + query
    recorder.feed(packet(frame[:1], tcp=True, seq=101), 101, 3)
    recorder.feed(packet(frame[1:9], tcp=True, seq=102), 101.01, 3.01)
    recorder.feed(packet(frame[9:], tcp=True, seq=110), 101.02, 3.02)
    response = struct.pack('!H', len(reply)) + reply
    recorder.feed(packet(response, True, True, seq=201), 101.1, 3.1)
    assert len(events) == 1 and events[0]['elapsed_ms'] == 100
    assert events[0]['transport'] == 'tcp'


def test_tcp_gap_never_guesses_a_new_frame():
    recorder, events = correlator('tcp')
    recorder.feed(packet(b'', tcp=True, seq=100, flags=2), 100, 2)
    frame = struct.pack('!H', len(dns())) + dns()
    recorder.feed(packet(frame, tcp=True, seq=105), 101, 3)
    assert recorder.counters['tcp_gaps'] == 1 and not events and not recorder.pending


def test_only_confirmed_local_entry_is_correlated_and_pending_is_bounded():
    recorder, events = correlator(local_addresses={'192.0.2.99'})
    recorder.feed(packet(dns()), 100, 2)
    assert not recorder.pending
    recorder, events = correlator()
    recorder.feed(packet(dns(), port=53), 100, 2)
    assert not recorder.pending
    for ident in range(MAX_PENDING + 3):
        recorder.feed(packet(dns(ident=ident)), 100, 2)
    assert len(recorder.pending) == MAX_PENDING and recorder.counters['pending_evictions'] == 3
    recorder.reset()
    assert not recorder.pending and recorder.counters['discarded_pending'] == MAX_PENDING


def test_compression_loops_and_malformed_packets_are_bounded():
    with pytest.raises(ValueError):
        name_at(b'\xc0\x00', 0)
    recorder, events = correlator()
    recorder.feed(packet(b'invalid'), 100, 2)
    assert recorder.counters['unparsed_packets'] == 1 and not events
    wire = bytearray(dns(True)); wire[2] |= 2
    assert dns_message(wire)['addresses_complete'] is False


def test_classic_bpf_filter_accepts_only_supported_ip_protocol_and_ports():
    # Execute the tiny fixed cBPF subset against actual test packets. Kernel
    # acceptance and live capture are tested separately on isolated Linux.
    code = filter_program([15453])
    def accepted(data):
        pc, accumulator, x = 0, 0, 0
        while pc < len(code):
            op, jt, jf, value = code[pc]
            if op in (0x30, 0x28, 0x48):
                start, width = value + (x if op == 0x48 else 0), 1 if op == 0x30 else 2
                if start + width > len(data):
                    return False
                accumulator = int.from_bytes(data[start:start + width], 'big')
            elif op == 0x54: accumulator &= value
            elif op == 0xb1: x = (data[value] & 15) * 4
            elif op in (0x15, 0x45):
                yes = accumulator == value if op == 0x15 else bool(accumulator & value)
                pc += jt if yes else jf
            elif op == 0x06: return bool(value)
            else: raise AssertionError(op)
            pc += 1
        return False
    for ipv6 in (False, True):
        for tcp in (False, True):
            assert accepted(packet(dns(), tcp=tcp, ipv6=ipv6))
            assert accepted(packet(dns(True), True, tcp, ipv6=ipv6))
            assert not accepted(packet(dns(), tcp=tcp, ipv6=ipv6, port=53))
    wire = bytearray(packet(dns())); wire[6] |= 0x20
    assert not accepted(wire)


def event(ident=1):
    recorder, events = correlator()
    now = time.time()
    recorder.feed(packet(dns(ident=ident)), now, 1)
    recorder.feed(packet(dns(True, ident=ident), True), now + .001, 1.001)
    return events[0]


def test_store_retention_literal_filters_pagination_and_restart(tmp_path):
    store = Store(tmp_path, max_rows=3, max_age=20)
    events = [event(i) for i in range(5)]
    store.append(events)
    assert store.summary()['retained_rows'] == 3
    first = store.recent(limit=2)
    assert len(first['events']) == 2 and first['next_before']
    assert len(store.recent(before=first['next_before'])['events']) == 1
    assert not store.recent(domain='%')['events']
    assert len(store.recent(source='192.0.2.2', domain='EXAMPLE', qtype=1)['events']) == 3
    old = event(); old['observed_at'] -= 60
    store.append([old]); store.prune()
    assert store.summary()['retained_rows'] == 2
    assert len(Store(tmp_path, max_rows=3).recent()['events']) == 2
    store.clear()
    assert not store.recent()['events']


def test_store_physical_cap_applies_to_every_new_connection(tmp_path):
    store = Store(tmp_path, max_bytes=128 * 1024)
    with store.connect() as db:
        assert db.execute('PRAGMA max_page_count').fetchone()[0] == 32
    for batch in range(10):
        store.append([event(batch * 100 + i) for i in range(100)])
    assert store.path.stat().st_size <= 128 * 1024
    assert store.path.with_name('events.sqlite-journal').stat().st_size == 0


def test_settings_default_off_persist_and_clear_discards_old_generation(tmp_path):
    state, run = tmp_path / 'state', tmp_path / 'run'
    state.mkdir(); run.mkdir()
    recorder = Recorder(state, run)
    assert not recorder.status()['enabled']
    recorder.configure(True)
    assert Recorder(state, run).enabled
    previous = recorder.generation
    recorder.request({'operation': 'clear'})
    assert recorder.generation != previous
    for _ in range(600): recorder.emit(previous, event())
    assert recorder.events.qsize() == 512 and recorder.counters['queue_drops'] == 88
    recorder.configure(False)
    assert not Recorder(state, run).enabled


@pytest.mark.parametrize('value', [{'operation': 'configure', 'enabled': 1}, {'operation': 'recent', 'limit': 101},
    {'operation': 'recent', 'before': True}, {'operation': 'recent', 'source': 'host.example'},
    {'operation': 'recent', 'filename': '/tmp/arbitrary'}, {'operation': 'shell'}, {'operation': 'recent', 'domain': 'a' * 254}])
def test_recording_protocol_rejects_unbounded_or_arbitrary_requests(value):
    with pytest.raises((ValueError, TypeError)): validate_request(value)


def test_sensitive_query_routes_are_admin_only_validate_and_use_shared_envelope(client, monkeypatch):
    api = client[0]
    monkeypatch.setenv('SUITE_READ_TOKEN', 'query-observer')
    calls = []
    def records(payload):
        calls.append(validate_request(payload))
        return {'enabled': False}
    monkeypatch.setattr(server.runtime_client, 'query_records_request', records)
    for path, method in [('/query-recording', 'get'), ('/query-recording', 'put'), ('/query-records', 'get'), ('/query-records', 'delete')]:
        assert getattr(api, method)('/api/v1' + path).status_code == 401
        assert getattr(api, method)('/api/v1' + path, headers={'Authorization': 'Bearer query-observer'}, json={'enabled': True}).status_code == 403
    assert api.put('/api/v1/query-recording', headers=AUTH, json={'enabled': True}).get_json()['data']['enabled'] is False
    assert calls[-1] == {'operation': 'configure', 'enabled': True}
    assert api.put('/api/v1/query-recording', headers=AUTH, json={'enabled': 'yes'}).status_code == 400
    assert api.get('/api/v1/query-records?limit=101', headers=AUTH).status_code == 400
    assert api.get('/api/v1/query-records?limit=1&limit=2', headers=AUTH).status_code == 400
    response = api.get('/api/v1/query-records?source=192.0.2.2&qtype=28', headers=AUTH)
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'
    assert calls[-1]['qtype'] == 28
    monkeypatch.setattr(server.runtime_client, 'query_records_request', lambda _: (_ for _ in ()).throw(RuntimeError('unavailable')))
    assert api.get('/api/v1/query-recording', headers=AUTH).get_json()['error']['code'] == 'UNAVAILABLE'


def test_browser_recording_writes_require_csrf_even_with_external_api_disabled(client, monkeypatch):
    from test_browser_auth import provision
    from browser_auth import set_external_api
    api, _, state = client
    csrf = provision(client)
    set_external_api(state, False)
    calls = []
    monkeypatch.setattr(server.runtime_client, 'query_records_request', lambda body: calls.append(body) or {'enabled': False})
    assert api.get('/api/v1/query-recording').status_code == 200
    for method, route in [('put', '/query-recording'), ('delete', '/query-records')]:
        assert getattr(api, method)('/api/v1' + route, json={'enabled': False}).status_code == 403
        assert getattr(api, method)('/api/v1' + route, json={'enabled': False}, headers={'X-CSRF-Token': csrf}).status_code == 200
    assert calls[-1]['operation'] == 'clear'


def test_stale_capture_status_never_claims_active(tmp_path):
    recorder = Recorder(tmp_path, tmp_path)
    recorder.configure(True)
    recorder.phase = 'recording'
    recorder.updated_at = time.time() - 6
    value = recorder.status()
    assert value['phase'] == 'unavailable' and not value['active'] and value['reason'] == 'recorder_stale'
