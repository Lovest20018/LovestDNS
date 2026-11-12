"""Attribute kernel-answer to client-read time using filtered receiver scheduling events."""

import argparse
from bisect import bisect_right
from collections import defaultdict
from itertools import groupby
import json
import math
from pathlib import Path

from dns_capture import WIRE_RECORD
from dnsperf_trace import read_client, split_timings


def analyze(prefix):
    meta = json.loads(Path(str(prefix) + '.json').read_text())
    if not meta['receiver_tid'] or meta['global_before'] != meta['global_after']:
        raise ValueError('Missing receiver or changed global trace configuration')
    clocks = meta['clocks']
    low = min(wall - last for first, wall, last in clocks)
    high = max(wall - first for first, wall, last in clocks)
    offset = (low + high) // 2
    uncertainty = (high - low) // 2 + 2000
    if uncertainty > 100_000:
        raise ValueError('Clock calibration uncertainty exceeds 100 microseconds')
    lines = Path(str(prefix) + '.events.tsv').read_text().splitlines()
    if lines[0] != 'mono_ns cpu event prev_state':
        raise ValueError('Unsupported scheduler trace format')
    events = [(int(t), kind, state) for t, cpu, kind, state in (line.split() for line in lines[1:])]
    starts, states = [], []
    if any(a[0] > b[0] for a, b in zip(events, events[1:])):
        raise ValueError('Receiver scheduling timestamps are unordered')
    current = 'unknown'
    ambiguous = 0
    for t, tied in groupby(events, key=lambda row: row[0]):
        tied = list(tied)
        if len(tied) > 1:
            # Printed ftrace timestamps have microsecond resolution. Do not
            # invent an order for simultaneous cross-CPU transitions.
            current = 'unknown'
            ambiguous += 1
            starts.append(t)
            states.append(current)
            continue
        _, kind, state = tied[0]
        if kind == 'in':
            current = 'running'
        elif kind == 'out':
            current = 'runnable' if state in ('R', 'R+') else 'sleeping'
        elif kind == 'wake' and current != 'running':
            current = 'runnable'
        elif kind != 'wake':
            raise ValueError('Unsupported receiver scheduling transition')
        starts.append(t)
        states.append(current)
    if len(starts) < 2 or len(events) != meta['events']:
        raise ValueError('Insufficient receiver scheduling events')
    phases = ('running', 'runnable', 'sleeping', 'unknown')
    cumulative = {name: [0] for name in phases}
    for i in range(1, len(starts)):
        for name in phases:
            cumulative[name].append(cumulative[name][-1] + (starts[i] - starts[i-1] if states[i-1] == name else 0))

    def area(t, phase):
        i = bisect_right(starts, t) - 1
        return cumulative[phase][i] + (t - starts[i] if states[i] == phase else 0)

    data_prefix = prefix.with_name('dnsperf')
    pairs = split_timings(read_client(data_prefix.with_suffix('.client.tsv'), 1_000_000),
                          WIRE_RECORD.iter_unpack(data_prefix.with_suffix('.wire.bin').read_bytes()))
    rows = []
    for ident, sent, query, reply, read, code in pairs:
        began, ended = reply * 1000 - offset, read * 1000 - offset
        if began < starts[0] or ended > starts[-1]:
            continue
        times = {phase: area(ended, phase) - area(began, phase) for phase in phases}
        assert sum(times.values()) == ended - began
        # Conservative inner interval is within every permitted clock mapping.
        core_a, core_b = began + uncertainty, ended - uncertainty
        confirmed = 'mixed_or_short'
        if core_a < core_b:
            for phase in phases[:-1]:
                lower = area(core_b, phase) - area(core_a, phase)
                # ftrace text has microsecond precision; allow 1us for each intersected transition.
                lower -= (bisect_right(starts, core_b) - bisect_right(starts, core_a) + 2) * 1000
                if lower > (ended - began) / 2:
                    confirmed = phase
                    break
        rows.append({'id': ident, 'client_us': read - sent, 'after_reply_us': read - reply,
                     'states_us': {name: value / 1000 for name, value in times.items()}, 'majority': confirmed})
    def summary(group):
        count = defaultdict(int)
        for row in group:
            count[row['majority']] += 1
        return {'samples': len(group), 'majority': dict(count),
                'median_us': {name: sorted(r['states_us'][name] for r in group)[(len(group)-1)//2]
                              for name in phases} if group else {}}
    slow = sorted(rows, key=lambda r: r['client_us'], reverse=True)[:math.ceil(len(rows) / 100)]
    return {'diagnostic_only': True, 'performance_accepted': False, 'clock_uncertainty_ns': uncertainty,
            'clock_offset_span_ns': high - low, 'trace_events': len(events), 'packet_samples': len(pairs),
            'ambiguous_timestamp_groups': ambiguous,
            'uncovered_packet_samples': len(pairs) - len(rows), 'covered': summary(rows),
            'slowest_covered_client_one_percent': summary(slow),
            'post_reply_at_least_1ms': summary([row for row in rows if row['after_reply_us'] >= 1000]),
            'limits': ['Running state includes interrupts and possible VM steal; it is not pure userspace CPU time.',
                       'Sleeping alone does not distinguish kernel delivery from application blocking.',
                       'Uncovered startup samples are retained in original packet metrics and unclassified here.']}


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('prefix', type=Path)
    args = cli.parse_args()
    print(json.dumps(analyze(args.prefix), indent=2))
