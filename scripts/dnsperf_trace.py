"""Join dnsperf's client timestamps to passive kernel UDP timestamps."""

from collections import defaultdict
import math
from pathlib import Path


METHOD = 'dnsperf-loopback-paired-us-v1'
MAX_TRACE = 1_000_000
RCODES = ('NOERROR', 'FORMERR', 'SERVFAIL', 'NXDOMAIN', 'NOTIMP', 'REFUSED',
          'YXDOMAIN', 'YXRRSET', 'NXRRSET', 'NOTAUTH', 'NOTZONE',
          'RCODE11', 'RCODE12', 'RCODE13', 'RCODE14', 'RCODE15')


def read_thread_affinity(path, requested, client):
    lines = Path(path).read_text(encoding='ascii').splitlines()
    if not lines or lines[0] != 'role cpu verified_us start_us' or len(lines) != 3 or not client:
        raise ValueError('Incomplete generator thread affinity audit')
    records = {}
    for line in lines[1:]:
        role, cpu, verified, started = line.split()
        cpu, verified, started = map(int, (cpu, verified, started))
        if role in records or requested.get(role) != cpu or not 0 < verified <= started <= min(r[1] for r in client):
            raise ValueError('Generator thread placement was not verified before the first query')
        records[role] = {'cpu': cpu, 'verified_us': verified, 'start_us': started}
    if set(records) != set(requested) or len({r['start_us'] for r in records.values()}) != 1:
        raise ValueError('Generator thread affinity roles or start times differ')
    return {'method': 'dnsperf-thread-affinity-before-start-v1', 'complete': True,
            'requested': requested, 'records': records}


def read_client(path, capacity):
    with Path(path).open(encoding='ascii') as stream:
        if stream.readline() != 'id sent_us read_us rcode\n':
            raise ValueError('Unexpected dnsperf timestamp trace format')
        rows = []
        for line in stream:
            ident, sent, received, code = map(int, line.split())
            if not (len(rows) < capacity and 0 <= ident <= 65535 and 0 < sent <= received
                    and 0 <= code < len(RCODES)):
                raise ValueError('Invalid dnsperf timestamp record or trace capacity exceeded')
            rows.append((ident, sent, received, code))
    return rows


def split_timings(client, wire):
    """Join reused IDs in order, requiring all four timestamps to be ordered.

    These timestamps use the same system realtime clock. Use integer microseconds
    on both sides to match dnsperf's gettimeofday() resolution.
    """
    by_id_client, by_id_wire = defaultdict(list), defaultdict(list)
    for ident, sent, received, code in client:
        by_id_client[ident].append((sent, received, code))
    for ident, query_ns, reply_ns in wire:
        if not 0 < query_ns <= reply_ns:
            raise ValueError('Kernel UDP timestamp reversal')
        by_id_wire[ident].append((query_ns, reply_ns))
    if not by_id_client or by_id_client.keys() != by_id_wire.keys():
        raise ValueError('Client and kernel trace IDs differ or are empty')
    samples = []
    for ident, endpoints in by_id_client.items():
        packets = by_id_wire[ident]
        if len(endpoints) != len(packets):
            raise ValueError(f'Client and kernel timestamp counts differ for DNS ID {ident}')
        previous_reply = 0
        for (sent, received, code), (query_ns, reply_ns) in zip(sorted(endpoints), sorted(packets)):
            query, reply = query_ns // 1000, reply_ns // 1000
            if not previous_reply <= sent <= query <= reply <= received:
                raise ValueError(f'Cannot prove timestamp ordering or ID reuse for DNS ID {ident}')
            previous_reply = received
            samples.append((ident, sent, query, reply, received, code))
    return sorted(samples, key=lambda row: (row[1], row[0]))


def timing_summary(paired):
    def summarize(values):
        ordered = sorted(values)
        return {key: ordered[math.ceil(len(ordered) * fraction) - 1] / 1000
                for key, fraction in (('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99))}

    phases = [(read - sent, query - sent, reply - query, read - reply)
              for _, sent, query, reply, read, _ in paired]
    tail = sorted(phases, key=lambda row: row[0], reverse=True)[:math.ceil(len(phases) / 100)]
    labels = ('client', 'before_kernel_query', 'kernel_roundtrip', 'after_kernel_reply')
    return {'method': METHOD, 'complete': True, 'samples': len(phases),
            **{name: summarize(values) for name, values in zip(labels, zip(*phases))},
            'slowest_client_one_percent': {'samples': len(tail),
                'median_ms': {name: summarize(values)['p50_ms']
                              for name, values in zip(labels, zip(*tail))},
                'post_reply_largest': sum(after > max(before, kernel) for _, before, kernel, after in tail)}}
