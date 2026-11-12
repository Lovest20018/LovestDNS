from pathlib import Path
import socket
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dns_capture import AnswerCapture, ArrivalTiming, WireLatency, TIMESTAMPNS, kernel_timestamp, udp_payload, validate_answer
from dns_lab import question


def answer(name='cold-0.cache.test', qtype=1, address=None, *, empty=False, flags=0x8180):
    query = question(name, qtype)
    value = socket.inet_pton(socket.AF_INET if qtype == 1 else socket.AF_INET6,
                             address or ('192.0.2.10' if qtype == 1 else '2001:db8::10'))
    rr = b'' if empty else b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 600, len(value)) + value
    return struct.pack('!6H', int.from_bytes(query[:2], 'big'), flags, 1, 0 if empty else 1, 0, 0) + query[12:] + rr


@pytest.mark.parametrize('qtype', [1, 28])
def test_validate_actual_positive_dns_answers(qtype):
    validate_answer(answer(qtype=qtype), {('cold-0.cache.test', qtype)}, {1: '192.0.2.10', 28: '2001:db8::10'})


@pytest.mark.parametrize('changes', [{'empty': True}, {'address': '192.0.2.99'}, {'flags': 0x8380},
                                     {'name': 'wrong.cache.test'}, {'qtype': 28}])
def test_noerror_is_not_enough_for_answer_correctness(changes):
    with pytest.raises(ValueError):
        validate_answer(answer(**changes), {('cold-0.cache.test', 1)}, {1: '192.0.2.10', 28: '2001:db8::10'})


def test_capture_matches_only_the_generator_and_test_resolver():
    payload = answer()
    ip = bytearray(20)
    ip[0], ip[9] = 0x45, 17
    ip[12:20] = socket.inet_aton('127.0.0.1') * 2
    packet = bytes(ip) + struct.pack('!4H', 5300, 43000, 8 + len(payload), 0) + payload
    assert udp_payload(packet, 5300, 43000) == (True, payload)
    assert udp_payload(packet, 5301, 43000) is None
    assert udp_payload(packet, 5300, 43001) is None
    with pytest.raises(ValueError):
        udp_payload(packet[:-1], 5300, 43000)


def test_kernel_arrivals_preserve_a_pause_and_burst_even_if_reader_is_delayed():
    timing = ArrivalTiming()
    for nanos in (0, 400_000_000, 400_100_000, 400_200_000):
        timing.observe(False, kernel_timestamp([(socket.SOL_SOCKET, TIMESTAMPNS, struct.pack('@ll', 100, nanos))]))
    timing.observe(True, 100_401_000_000)
    query = timing.result()['streams']['query']
    assert query['count'] == 4 and query['max_gap_ns'] == 400_000_000
    assert query['min_gap_ns'] == 100_000
    assert query['peak_packets_per_1ms'] == 3
    assert timing.result()['streams']['answer']['count'] == 1


@pytest.mark.parametrize('value', [None, b'', struct.pack('@ll', 1, 1_000_000_000)])
def test_missing_or_malformed_kernel_timestamps_cannot_become_timing_evidence(value):
    controls = [] if value is None else [(socket.SOL_SOCKET, TIMESTAMPNS, value)]
    with pytest.raises(ValueError, match='kernel packet timestamp'):
        kernel_timestamp(controls)


def test_wire_timing_pairs_reused_ids_by_question_and_does_not_measure_reader_delay():
    timing = WireLatency()
    for name, stamp in [('first.cache.test', 0), ('second.cache.test', 1_000_000)]:
        timing.observe(False, question(name, identifier=12), stamp)
    # Capture decoding may occur much later; only the supplied kernel stamps count.
    for name, stamp in [('second.cache.test', 3_000_000), ('first.cache.test', 4_000_000)]:
        packet = b'\x00\x0c' + answer(name)[2:]
        timing.observe(True, packet, stamp)
    result = timing.result(2, 2)
    assert result['complete'] and result['samples'] == 2
    assert result['p50_ms'] == 2 and result['p99_ms'] == 4


@pytest.mark.parametrize('problem', ['collision', 'unmatched', 'clock_reversal'])
def test_wire_timing_refuses_ambiguous_pairs_and_clock_steps(problem):
    timing = WireLatency();packet = question('cold.cache.test', identifier=1)
    if problem != 'unmatched':
        timing.observe(False, packet, 1000)
    if problem == 'collision':
        timing.observe(False, packet, 2000)
    timing.observe(True, packet, 500 if problem == 'clock_reversal' else 3000)
    assert not timing.result(1, 1)['complete']


def test_wire_timing_uses_kernel_times_even_when_answer_is_captured_before_its_question():
    timing = WireLatency(); packet = question('reordered.cache.test', identifier=7)
    timing.observe(True, packet, 3000000)
    assert not timing.result(0, 1)['complete']
    timing.observe(False, packet, 1000000)
    result = timing.result(1, 1)
    assert result['complete'] and result['samples'] == 1 and result['p99_ms'] == 2
    assert result['capture_order_reversed_pairs'] == 1
    assert result['unmatched_answers'] == result['pending_queries'] == 0


@pytest.mark.parametrize('problem', ['clock_reversal', 'duplicate_answer', 'wrong_question'])
def test_reordered_capture_does_not_hide_unmatched_or_ambiguous_packets(problem):
    timing = WireLatency(); packet = question('reordered.cache.test', identifier=7)
    timing.observe(True, packet, 3000000)
    if problem == 'duplicate_answer': timing.observe(True, packet, 3100000)
    timing.observe(False, question('wrong.cache.test', identifier=7) if problem == 'wrong_question' else packet,
                   4000000 if problem == 'clock_reversal' else 1000000)
    assert not timing.result(1, 1)['complete']


def test_capture_does_not_silently_skip_when_permission_is_missing(tmp_path, monkeypatch):
    data = tmp_path / 'input.txt'
    data.write_text('cold-0.cache.test A\n')
    def denied(*args):
        raise PermissionError('Missing CAP_NET_RAW')
    monkeypatch.setattr(socket, 'socket', denied)
    with pytest.raises(RuntimeError, match='CAP_NET_RAW'):
        with AnswerCapture(5300, 43000, data, {1: '192.0.2.10'}):
            pytest.fail('Capture was silently skipped')
