"""Private, receiver-filtered ftrace instance for a finite DNS diagnostic."""

import json
from pathlib import Path
import re
import time
import uuid


TRACE_ROOT = Path('/sys/kernel/tracing')


def ns(decimal):
    whole, fraction = decimal.split('.')
    return int(whole) * 1_000_000_000 + int(fraction.ljust(9, '0'))


def receiver_events(raw, tid):
    """Retain only target TID transitions; discard other task identities."""
    result = []
    for line in raw.splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.search(r'\[(\d+)\].*? (\d+\.\d+): (sched_switch|sched_wakeup): (.*)', line)
        if not match:
            raise ValueError('Unexpected receiver scheduling event format')
        cpu, timestamp, event, fields = match.groups()
        if event == 'sched_switch':
            prev = re.search(r'\bprev_pid=(\d+).*?prev_state=(\S+)', fields)
            next_pid = re.search(r'\bnext_pid=(\d+)', fields)
            if not prev or not next_pid:
                raise ValueError('Incomplete sched_switch')
            if int(prev[1]) == tid:
                kind, state = 'out', prev[2]
            elif int(next_pid[1]) == tid:
                kind, state = 'in', '-'
            else:
                raise ValueError('Trace filter captured an unrelated context switch')
        else:
            target = re.search(r'\bpid=(\d+)\b', fields)
            if not target or int(target[1]) != tid:
                raise ValueError('Trace filter captured an unrelated wakeup')
            kind, state = 'wake', '-'
        result.append((ns(timestamp), int(cpu), kind, state))
    return sorted(result, key=lambda row: row[0])


class ReceiverTrace:
    def __init__(self, output):
        self.output = Path(output)
        self.root = TRACE_ROOT / 'instances' / ('ldns-' + uuid.uuid4().hex[:16])
        self.tid = None
        self.clocks = []

    def write(self, name, value):
        (self.root / name).write_text(str(value))

    def clock(self):
        first = time.monotonic_ns()
        wall = time.time_ns()
        last = time.monotonic_ns()
        self.clocks.append((first, wall, last))

    def __enter__(self):
        self.global_before = {n: (TRACE_ROOT / n).read_text() for n in ('tracing_on', 'current_tracer', 'trace_clock')}
        self.root.mkdir(mode=0o700)
        try:
            with Path(str(self.output) + '.instance.json').open('x') as marker:
                json.dump({'instance': self.root.name}, marker)
            self.write('tracing_on', '0')
            self.write('trace_clock', 'mono')
            self.write('buffer_size_kb', '8192')
            # A target wakeup can be emitted by a different CPU. Keep the
            # instance's all-CPU mask and filter by target TID instead.
            self.clock()
        except BaseException:
            self.close()
            raise
        return self

    def observe(self, pid):
        self.clock()
        if self.tid:
            return
        for p in Path('/proc', str(pid), 'task').glob('[0-9]*'):
            try:
                name = (p / 'comm').read_text().strip()
            except FileNotFoundError:
                continue
            if name == 'perf-recv-0000':
                self.tid = int(p.name)
                self.write('events/sched/sched_switch/filter', f'prev_pid == {self.tid} || next_pid == {self.tid}')
                self.write('events/sched/sched_wakeup/filter', f'pid == {self.tid}')
                self.write('events/sched/sched_switch/enable', '1')
                self.write('events/sched/sched_wakeup/enable', '1')
                self.write('tracing_on', '1')
                self.clock()
                return

    def close(self):
        if not self.root.exists():
            return
        self.write('tracing_on', '0')
        self.write('events/enable', '0')
        self.root.rmdir()

    def __exit__(self, *_):
        try:
            self.write('tracing_on', '0')
            self.clock()
            stats = {p.parent.name: p.read_text() for p in (self.root / 'per_cpu').glob('cpu*/stats')}
            events = receiver_events((self.root / 'trace').read_text(), self.tid)
            Path(str(self.output) + '.events.tsv').write_text('mono_ns cpu event prev_state\n' +
                ''.join(f'{stamp} {cpu} {kind} {state}\n' for stamp, cpu, kind, state in events))
            metadata = {'receiver_tid': self.tid, 'events': len(events), 'clocks': self.clocks,
                        'instance': self.root.name, 'per_cpu_stats': stats,
                        'tracing_cpumask': (self.root / 'tracing_cpumask').read_text().strip(),
                        'global_before': self.global_before,
                        'global_after': {n: (TRACE_ROOT / n).read_text() for n in self.global_before}}
            Path(str(self.output) + '.json').write_text(json.dumps(metadata, indent=2) + '\n')
            if not self.tid or not events or metadata['global_before'] != metadata['global_after']:
                raise ValueError('Missing receiver trace or changed global trace configuration')
            for value in stats.values():
                if any(int(n) for n in re.findall(r'^(?:overrun|commit overrun|dropped events):\s*(\d+)', value, re.M)):
                    raise ValueError('Receiver scheduling trace buffer dropped events')
        finally:
            self.close()
