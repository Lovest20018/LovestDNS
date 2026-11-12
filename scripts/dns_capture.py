"""Passively verify every UDP answer in a disposable loopback DNS benchmark."""
from collections import Counter
import math
import multiprocessing
from pathlib import Path
import socket
import struct
import threading
import time

from dns_lab import parse, read_name
from dns_cpu import pin

METHOD = 'loopback-udp-all-answers-v1'
TIMESTAMPNS = 35  # Linux SO_TIMESTAMPNS_OLD, using native timespec layout.
WIRE_RECORD = struct.Struct('<HQQ')  # DNS ID, kernel query ns, kernel answer ns


def kernel_timestamp(controls):
    for level, kind, value in controls:
        if level == socket.SOL_SOCKET and kind == TIMESTAMPNS:
            if len(value) != struct.calcsize('@ll'):
                raise ValueError('Invalid kernel packet timestamp')
            seconds, nanos = struct.unpack('@ll', value)
            if seconds < 0 or not 0 <= nanos < 1_000_000_000:
                raise ValueError('Invalid kernel packet timestamp')
            return seconds * 1_000_000_000 + nanos
    raise ValueError('Missing kernel packet timestamp')


class ArrivalTiming:
    """Kernel arrival times distinguish sender bursts from a delayed reader."""
    def __init__(self):
        self.streams = {name: {'count': 0, 'first_ns': None, 'last_ns': None,
                              'min_gap_ns': None, 'max_gap_ns': 0, 'out_of_order': 0, 'bins': Counter()}
                        for name in ('query', 'answer')}

    def observe(self, response, stamp):
        row = self.streams['answer' if response else 'query']
        previous = row['last_ns']
        if previous is not None:
            row['max_gap_ns'] = max(row['max_gap_ns'], stamp - previous)
            gap = stamp - previous
            row['min_gap_ns'] = gap if row['min_gap_ns'] is None else min(row['min_gap_ns'], gap)
            row['out_of_order'] += stamp < previous
        row['first_ns'] = stamp if row['first_ns'] is None else min(row['first_ns'], stamp)
        row['last_ns'] = stamp if previous is None else max(previous, stamp)
        row['count'] += 1
        row['bins'][stamp // 1_000_000] += 1

    def result(self):
        return {'method': 'linux-kernel-so-timestampns-v1', 'streams': {
            name: {**{key: value for key, value in row.items() if key != 'bins'},
                   'peak_packets_per_1ms': max(row['bins'].values(), default=0),
                   'densest_bins': row['bins'].most_common(5)}
            for name, row in self.streams.items()}}


class WireLatency:
    """Correlate kernel-observed UDP requests and replies, independent of recv()."""
    def __init__(self, trace_capacity=0):
        self.pending, self.early_answers, self.histogram = {}, {}, Counter()
        self.collisions = self.clock_reversals = self.reordered_pairs = 0
        self.trace = bytearray(trace_capacity * WIRE_RECORD.size) if trace_capacity else None
        self.trace_offset = 0

    def pair(self, ident, began, ended):
        if ended < began:
            self.clock_reversals += 1
        else:
            self.histogram[(ended - began + 999) // 1000] += 1
            if self.trace is not None:
                if self.trace_offset == len(self.trace):
                    raise ValueError('Kernel timestamp trace capacity exceeded')
                WIRE_RECORD.pack_into(self.trace, self.trace_offset, ident, began, ended)
                self.trace_offset += WIRE_RECORD.size

    def observe(self, response, packet, stamp):
        name, end = read_name(packet, 12)
        qtype, qclass = struct.unpack('!HH', packet[end:end + 4])
        key = (int.from_bytes(packet[:2], 'big'), name, qtype, qclass)
        if not response:
            ended = self.early_answers.pop(key, None)
            if ended is not None:
                self.reordered_pairs += 1
                self.pair(key[0], stamp, ended)
                return
            self.collisions += key in self.pending
            self.pending[key] = stamp
            return
        began = self.pending.pop(key, None)
        if began is None:
            self.collisions += key in self.early_answers
            self.early_answers[key] = stamp
        else:
            self.pair(key[0], began, stamp)

    def result(self, queries, answers):
        count = sum(self.histogram.values())
        def percentile(fraction):
            rank, seen = math.ceil(count * fraction), 0
            if not count:
                return None
            for micros, total in sorted(self.histogram.items()):
                seen += total
                if seen >= rank:
                    return micros / 1000
        return {'method': 'linux-kernel-udp-roundtrip-us-v2', 'samples': count,
                'p50_ms': percentile(.5), 'p95_ms': percentile(.95), 'p99_ms': percentile(.99),
                'pending_queries': len(self.pending), 'collisions': self.collisions,
                'unmatched_answers': len(self.early_answers), 'clock_reversals': self.clock_reversals,
                'capture_order_reversed_pairs': self.reordered_pairs,
                'complete': (count == answers and queries == answers + len(self.pending)
                             and not (self.collisions or self.early_answers or self.clock_reversals))}


def validate_answer(packet, questions, expected):
    """These benchmarks contain positive A/AAAA questions, with one fixed RR."""
    _, flags, qd, _, _, _ = struct.unpack('!6H', packet[:12])
    name, end = read_name(packet, 12)
    qtype, qclass = struct.unpack('!HH', packet[end:end + 4])
    if not flags & 0x8000 or qd != 1 or qclass != 1 or (name, qtype) not in questions:
        raise ValueError('Response question differs from the benchmark input')
    result = parse(packet)
    wanted = [(name, qtype, 1, expected[qtype])]
    actual = [(rr['name'], rr['type'], rr['class'], rr['value']) for rr in result['answers']]
    if result['rcode'] or result['truncated'] or actual != wanted:
        raise ValueError('Response does not contain the expected A/AAAA answer')


def udp_payload(packet, server_port, client_port):
    """AF_PACKET/SOCK_DGRAM supplies an IPv4 packet without its Ethernet header."""
    if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != 17:
        return None
    offset = (packet[0] & 15) * 4
    if offset < 20 or len(packet) < offset + 8 or packet[12:20] != b'\x7f\x00\x00\x01' * 2:
        return None
    source, target, length, _ = struct.unpack('!4H', packet[offset:offset + 8])
    if (source, target) not in {(server_port, client_port), (client_port, server_port)}:
        return None
    if int.from_bytes(packet[6:8], 'big') & 0x3fff or length < 20 or offset + length > len(packet):
        raise ValueError('Fragmented or truncated benchmark UDP packet')
    return source == server_port, packet[offset + 8:offset + length]


class AnswerCapture:
    """No proxy or extra DNS queries are inserted into the measured path.

    Linux CAP_NET_RAW (normally root) is required. Missing permission, capture
    drops and count mismatches invalidate evidence rather than skipping checks.
    """
    def __init__(self, server_port, client_port, data, expected, trace_path=None, trace_capacity=0):
        self.server_port, self.client_port, self.expected = server_port, client_port, expected
        kinds = {'A': 1, 'AAAA': 28}
        self.questions = {(name, kinds[kind]) for name, kind in
                          (line.split() for line in Path(data).read_text().splitlines() if line.strip())}
        self.checked = self.errors = self.queries_seen = 0
        self.types = Counter()
        self.examples, self.failure = [], None
        self.arrivals = ArrivalTiming()
        self.wire_latency = WireLatency(trace_capacity)
        self.trace_path = trace_path
        self.stop = threading.Event()
        self.stop_at = None

    def __enter__(self):
        try:
            self.socket = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x800))
        except (AttributeError, PermissionError) as exc:
            raise RuntimeError('Answer verification requires Linux CAP_NET_RAW; run the isolated benchmark as root') from exc
        try:
            self.socket.bind(('lo', 0))
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, TIMESTAMPNS, 1)
            self.socket.settimeout(.1)
            self.thread = threading.Thread(target=self.receive, daemon=True)
            self.thread.start()
        except BaseException:
            self.socket.close()
            raise
        return self

    def receive(self):
        try:
            while self.stop_at is None or time.monotonic() - self.stop_at < .5:
                try:
                    packet, controls, flags, address = self.socket.recvmsg(65535, socket.CMSG_SPACE(struct.calcsize('@ll')))
                except socket.timeout:
                    if self.stop.is_set():
                        return
                    continue
                if address[2] == 4:  # PACKET_OUTGOING: loopback's duplicate copy.
                    continue
                matched = udp_payload(packet, self.server_port, self.client_port)
                if matched is None:
                    continue
                if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                    raise ValueError('Truncated benchmark packet or timestamp')
                response, payload = matched
                stamp = kernel_timestamp(controls)
                self.arrivals.observe(response, stamp)
                self.wire_latency.observe(response, payload, stamp)
                if not response:
                    self.queries_seen += 1
                    continue
                self.checked += 1
                try:
                    validate_answer(payload, self.questions, self.expected)
                    _, end = read_name(payload, 12)
                    self.types[struct.unpack('!H', payload[end:end + 2])[0]] += 1
                except (ValueError, IndexError, KeyError, struct.error, OSError) as exc:
                    self.errors += 1
                    if len(self.examples) < 5:
                        self.examples.append(str(exc))
        except Exception as exc:
            self.failure = str(exc)

    def __exit__(self, *args):
        self.stop_at = time.monotonic()
        self.stop.set()
        self.thread.join(timeout=2)
        try:
            if self.thread.is_alive():
                self.failure = 'Answer capture did not stop within its bound'
            # PACKET_STATISTICS: packet and drop counters for this capture socket.
            self.packets, self.drops = struct.unpack_from('=2I', self.socket.getsockopt(263, 6, 12))
        finally:
            self.socket.close()
        if self.trace_path is not None and not self.failure:
            trace = self.wire_latency
            with Path(self.trace_path).open('xb') as stream:
                stream.write(memoryview(trace.trace)[:trace.trace_offset])

    def result(self, sent, completed):
        return {'method': METHOD, 'queries_seen': self.queries_seen, 'checked': self.checked,
                'errors': self.errors, 'types': dict(self.types), 'capture_drops': self.drops,
                'error_examples': self.examples, 'capture_error': self.failure,
                'arrival_timing': self.arrivals.result(),
                'wire_latency': self.wire_latency.result(self.queries_seen, self.checked),
                'complete': not self.failure and self.drops == 0 and self.queries_seen == sent and self.checked == completed}


def capture_worker(connection, arguments, cpu=None):
    try:
        pin(cpu)
        with AnswerCapture(*arguments) as capture:
            connection.send({'ready': True})
            if connection.recv() != 'stop':
                raise ValueError('Unexpected capture control message')
        connection.send(capture.result(capture.queries_seen, capture.checked))
    except Exception as exc:
        connection.send({'capture_error': str(exc)})
    finally:
        connection.close()


class CaptureProcess:
    """Keep packet decoding off the controlled upstream's Python interpreter."""
    def __init__(self, *arguments, cpu=None):
        self.arguments = arguments
        self.cpu = cpu

    def __enter__(self):
        context = multiprocessing.get_context('spawn')
        self.connection, peer = context.Pipe()
        self.process = context.Process(target=capture_worker, args=(peer, self.arguments, self.cpu), daemon=True)
        self.process.start()
        peer.close()
        try:
            if not self.connection.poll(10):
                raise RuntimeError('Answer capture did not become ready within 10 seconds')
            message = self.connection.recv()
            if not message.get('ready'):
                raise RuntimeError(message.get('capture_error', 'Answer capture failed to start'))
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)
        self.connection.close()

    def __exit__(self, *args):
        try:
            self.connection.send('stop')
            if not self.connection.poll(5):
                raise RuntimeError('Answer capture did not finish within 5 seconds')
            self.evidence = self.connection.recv()
            if 'complete' not in self.evidence:
                raise RuntimeError(self.evidence.get('capture_error', 'Missing answer evidence'))
        finally:
            self.close()

    def result(self, sent, completed):
        return {**self.evidence, 'complete': self.evidence['complete'] and
                self.evidence['queries_seen'] == sent and self.evidence['checked'] == completed}


def client_udp_sample(port):
    """Sample one loopback UDP socket without changing its buffer settings."""
    for line in Path('/proc/net/udp').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f'0100007F:{port:04X}':
            return {'rx_queue_bytes': int(fields[4].split(':')[1], 16), 'drops': int(fields[-1])}
    return None
