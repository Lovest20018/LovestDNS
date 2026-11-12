"""Bounded passive DNS decoding. No forwarding, DNS queries, or raw packet storage."""
from collections import Counter, OrderedDict
import ipaddress
import struct
import uuid

MAX_PENDING = 2048
MAX_STREAMS = 128  # Directional streams, each at most one 65535-byte DNS frame.
MAX_FRAME = 65537
WAIT_SECONDS = 10
TYPES = {1: 'A', 2: 'NS', 5: 'CNAME', 6: 'SOA', 12: 'PTR', 15: 'MX',
         16: 'TXT', 28: 'AAAA', 33: 'SRV', 64: 'SVCB', 65: 'HTTPS', 255: 'ANY'}
RCODES = {0: 'NOERROR', 1: 'FORMERR', 2: 'SERVFAIL', 3: 'NXDOMAIN',
          4: 'NOTIMP', 5: 'REFUSED', 16: 'BADVERS'}


def name_at(data, offset):
    labels, seen, end, length = [], set(), None, 1
    for _ in range(128):
        if offset >= len(data) or offset in seen:
            raise ValueError('invalid_dns_name')
        seen.add(offset)
        size = data[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            if offset >= len(data):
                raise ValueError('invalid_dns_pointer')
            if end is None:
                end = offset + 1
            offset = ((size & 0x3f) << 8) | data[offset]
            continue
        if size & 0xc0 or size > 63 or offset + size > len(data):
            raise ValueError('invalid_dns_label')
        if not size:
            return '.'.join(labels) + '.', end or offset
        length += size + 1
        if length > 255:
            raise ValueError('long_dns_name')
        # Escaping preserves label boundaries and avoids decoding arbitrary bytes.
        labels.append(''.join(chr(b).lower() if 33 <= b <= 126 and b not in (46, 92)
                              else '\\%03d' % b for b in data[offset:offset + size]))
        offset += size
    raise ValueError('dns_pointer_limit')


def dns_message(data):
    if len(data) < 12:
        raise ValueError('short_dns')
    ident, flags, qd, an, ns, ar = struct.unpack_from('!6H', data)
    if qd != 1 or flags & 0x7800:  # QUERY opcode only; no fabricated multi-question rows.
        raise ValueError('unsupported_dns_question')
    name, offset = name_at(data, 12)
    if offset + 4 > len(data):
        raise ValueError('short_dns_question')
    qtype, qclass = struct.unpack_from('!HH', data, offset)
    offset += 4
    response = bool(flags & 0x8000)
    result = {'dns_id': ident, 'domain': name, 'qtype': qtype, 'qclass': qclass,
              'response': response, 'rcode': flags & 15 if response else None,
              'truncated': bool(flags & 0x200), 'answer_count': an if response else None,
              'addresses': [], 'addresses_complete': True}
    if not response:
        return result
    # Parse complete sections to recognize extended RCODE. Large/TC responses
    # retain the observed header but never claim a complete address list.
    if an + ns + ar > 256 or result['truncated']:
        result['addresses_complete'] = False
        return result
    try:
        for index in range(an + ns + ar):
            _, offset = name_at(data, offset)
            if offset + 10 > len(data):
                raise ValueError('short_rr')
            kind, cls, ttl, size = struct.unpack_from('!HHIH', data, offset)
            offset += 10
            if offset + size > len(data):
                raise ValueError('short_rdata')
            if index < an and cls == 1 and (kind, size) in {(1, 4), (28, 16)}:
                address = str(ipaddress.ip_address(data[offset:offset + size]))
                if address not in result['addresses']:
                    if len(result['addresses']) < 4:
                        result['addresses'].append(address)
                    else:
                        result['addresses_complete'] = False
            if index >= an + ns and kind == 41:
                result['rcode'] |= (ttl >> 24) << 4
            offset += size
    except ValueError:
        result['addresses'] = []
        result['addresses_complete'] = False
    return result


def ip_packet(data):
    if not data:
        raise ValueError('short_ip')
    version = data[0] >> 4
    if version == 4:
        if len(data) < 20:
            raise ValueError('short_ipv4')
        offset = (data[0] & 15) * 4
        size = struct.unpack_from('!H', data, 2)[0]
        if offset < 20 or size < offset or size > len(data):
            raise ValueError('short_ipv4')
        if struct.unpack_from('!H', data, 6)[0] & 0x3fff:
            raise ValueError('fragmented_ip')
        protocol, source, target = data[9], data[12:16], data[16:20]
    elif version == 6:
        if len(data) < 40:
            raise ValueError('short_ipv6')
        offset, size = 40, 40 + struct.unpack_from('!H', data, 4)[0]
        if size > len(data):
            raise ValueError('short_ipv6')
        protocol, source, target = data[6], data[8:24], data[24:40]
        if protocol not in (6, 17):
            raise ValueError('ipv6_extension_or_protocol')
    else:
        raise ValueError('unsupported_ip')
    payload = data[offset:size]
    if protocol not in (6, 17) or len(payload) < (20 if protocol == 6 else 8):
        raise ValueError('short_transport')
    sport, dport = struct.unpack_from('!HH', payload)
    result = {'source': str(ipaddress.ip_address(source)), 'target': str(ipaddress.ip_address(target)),
              'sport': sport, 'dport': dport, 'transport': 'tcp' if protocol == 6 else 'udp'}
    if protocol == 17:
        length = struct.unpack_from('!H', payload, 4)[0]
        if length < 8 or length > len(payload):
            raise ValueError('short_udp')
        result['payload'] = payload[8:length]
    else:
        start = (payload[12] >> 4) * 4
        if start < 20 or start > len(payload):
            raise ValueError('short_tcp')
        result.update(seq=struct.unpack_from('!I', payload, 4)[0], flags=payload[13], payload=payload[start:])
    return result


class Correlator:
    def __init__(self, endpoints, emit, counters=None, session=None, local_addresses=None):
        self.endpoints, self.emit = endpoints, emit
        self.counters = counters if counters is not None else Counter()
        self.session = session or uuid.uuid4().hex
        self.pending, self.streams = OrderedDict(), OrderedDict()
        self.serial = 0
        self.local_addresses = local_addresses

    def reset(self):
        self.counters['discarded_pending'] += len(self.pending)
        self.pending.clear()
        self.streams.clear()

    def endpoint(self, packet, reply):
        address, port = (packet['source'], packet['sport']) if reply else (packet['target'], packet['dport'])
        for item in self.endpoints:
            if item['transport'] != packet['transport'] or item['port'] != port:
                continue
            configured = ipaddress.ip_address(item['address'])
            actual = ipaddress.ip_address(address)
            if self.local_addresses is not None and address not in self.local_addresses and not actual.is_loopback:
                continue
            # IPv6 wildcard sockets can also own IPv4 service traffic.
            if str(configured) == address or configured.is_unspecified and (configured.version == actual.version or configured.version == 6):
                return item
        return None

    def feed(self, data, wall, mono):
        try:
            packet = ip_packet(data)
            if not self.endpoint(packet, False) and not self.endpoint(packet, True):
                return
            if packet['transport'] == 'udp':
                self.message(packet, packet['payload'], wall, mono)
            else:
                self.tcp(packet, wall, mono)
        except (ValueError, struct.error):
            self.counters['unparsed_packets'] += 1

    def tcp(self, packet, wall, mono):
        key = tuple(packet[k] for k in ('source', 'target', 'sport', 'dport'))
        reverse = (key[1], key[0], key[3], key[2])
        flags, seq, payload = packet['flags'], packet['seq'], packet['payload']
        if flags & 2:
            if not flags & 16:
                self.streams.pop(reverse, None)
            self.streams[key] = {'next': (seq + 1) & 0xffffffff, 'data': bytearray(),
                                 'wall': wall, 'mono': mono, 'seen': mono}
            self.streams.move_to_end(key)
            while len(self.streams) > MAX_STREAMS:
                self.streams.popitem(last=False)
                self.counters['stream_evictions'] += 1
            seq = (seq + 1) & 0xffffffff
        stream = self.streams.get(key)
        if payload and stream is None:
            self.counters['tcp_untracked_packets'] += 1
        if payload and stream is not None:
            delta = (seq - stream['next'] + (1 << 31)) % (1 << 32) - (1 << 31)
            if delta > 0:
                self.streams.pop(key, None)
                self.counters['tcp_gaps'] += 1
                return  # Wait for a new handshake; never guess DNS frame alignment.
            if delta < 0:
                payload = payload[min(-delta, len(payload)):]
                self.counters['tcp_retransmitted_packets'] += 1
            if payload:
                if not stream['data']:
                    stream['wall'], stream['mono'] = wall, mono
                stream['next'] = (stream['next'] + len(payload)) & 0xffffffff
                stream['seen'] = mono
                stream['data'].extend(payload)
                if len(stream['data']) > MAX_FRAME:
                    self.streams.pop(key, None)
                    self.counters['stream_evictions'] += 1
                    return
                while len(stream['data']) >= 2:
                    length = struct.unpack_from('!H', stream['data'])[0]
                    if length < 12:
                        self.streams.pop(key, None)
                        self.counters['unparsed_packets'] += 1
                        return
                    if len(stream['data']) < length + 2:
                        break
                    message = bytes(stream['data'][2:length + 2])
                    del stream['data'][:length + 2]
                    self.message(packet, message, stream['wall'], stream['mono'])
                    stream['wall'], stream['mono'] = wall, mono
        if flags & 5:
            self.streams.pop(key, None)
            if flags & 4:
                self.streams.pop(reverse, None)

    def message(self, packet, data, wall, mono):
        message = dns_message(data)
        reply = message['response']
        endpoint = self.endpoint(packet, reply)
        if endpoint is None:
            return
        client = (packet['target'], packet['dport']) if reply else (packet['source'], packet['sport'])
        server = (packet['source'], packet['sport']) if reply else (packet['target'], packet['dport'])
        key = (packet['transport'], client, server, message['dns_id'], message['domain'], message['qtype'], message['qclass'])
        if not reply:
            if key in self.pending:
                self.pending[key]['same_key_packets'] += 1
                self.counters['same_key_queries'] += 1
                return
            self.serial += 1
            self.pending[key] = {'id': f'{self.session}:{self.serial}', 'observed_at': wall,
                '_mono': mono, 'source_ip': client[0], 'source_port': client[1],
                'entry_address': server[0], 'entry_port': server[1], 'component': endpoint['component'],
                'transport': packet['transport'], 'domain': message['domain'], 'qtype': message['qtype'],
                'record_type': TYPES.get(message['qtype'], str(message['qtype'])), 'qclass': message['qclass'],
                'same_key_packets': 1, 'rcode': None, 'rcode_value': None, 'answer_count': None,
                'addresses': [], 'addresses_complete': False, 'truncated': None,
                'elapsed_ms': None, 'response_at': None, 'outcome': 'no_response_observed',
                'timing_scope': 'passive_entry_packet_interval', 'branch': None, 'cache_hit': None}
            self.counters['queries_observed'] += 1
            while len(self.pending) > MAX_PENDING:
                self.pending.popitem(last=False)
                self.counters['pending_evictions'] += 1
        else:
            event = self.pending.pop(key, None)
            if event is None:
                self.counters['unmatched_responses'] += 1
                return
            elapsed = (wall - event['observed_at']) * 1000
            if not 0 <= elapsed <= WAIT_SECONDS * 1000:
                self.counters['invalid_timing'] += 1
                return
            event.update(response_at=wall, elapsed_ms=round(elapsed, 3), outcome='response_observed',
                         rcode=RCODES.get(message['rcode'], str(message['rcode'])), rcode_value=message['rcode'],
                         answer_count=message['answer_count'], addresses=message['addresses'],
                         addresses_complete=message['addresses_complete'], truncated=message['truncated'])
            self.publish(event)

    def publish(self, event):
        event.pop('_mono', None)
        self.emit(event)

    def expire(self, mono):
        while self.pending:
            key, event = next(iter(self.pending.items()))
            if mono - event['_mono'] < WAIT_SECONDS:
                break
            del self.pending[key]
            self.publish(event)
            self.counters['no_response_observed'] += 1
        for key, stream in list(self.streams.items()):
            if mono - stream['seen'] > WAIT_SECONDS:
                del self.streams[key]
                self.counters['tcp_idle_expirations'] += 1
