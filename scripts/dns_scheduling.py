"""Bounded Linux scheduling evidence for disposable tests, never acceptance."""
import json
import os
from pathlib import Path
import threading
import time


def task_stat(raw):
    fields = raw.rsplit(')', 1)[1].split()
    return {'state': fields[0], 'cpu_ticks': int(fields[11]) + int(fields[12]),
            'start_ticks': int(fields[19]), 'processor': int(fields[36])}


def counters(raw):
    return {key: int(value) for key, value in (line.split() for line in raw.splitlines())}


def cgroup_path():
    rows = Path('/proc/self/cgroup').read_text().splitlines()
    relative = next(row[3:] for row in rows if row.startswith('0::'))
    return Path('/sys/fs/cgroup') / relative.lstrip('/')


def snapshot(group):
    began = time.monotonic_ns()
    data = {'monotonic_ns': began, 'unix_ns': time.time_ns(), 'tasks': {}}
    data['sched_schedstats'] = Path('/proc/sys/kernel/sched_schedstats').read_text().strip()
    for name in ('cpu.stat', 'cpu.pressure', 'memory.events', 'memory.current'):
        try:
            data[name] = (group / name).read_text()
        except OSError as error:
            data[name + '_error'] = type(error).__name__
    data['host_cpu'] = [line for line in Path('/proc/stat').read_text().splitlines() if line.startswith('cpu')]
    data['host_cpu_pressure'] = Path('/proc/pressure/cpu').read_text()
    for pid in (group / 'cgroup.procs').read_text().split():
        try:
            tasks = list((Path('/proc') / pid / 'task').iterdir())
        except FileNotFoundError:
            continue
        for task in tasks:
            try:
                stats = task_stat((task / 'stat').read_text())
                sched = [int(value) for value in (task / 'schedstat').read_text().split()]
                data['tasks'][pid + ':' + task.name] = {**stats, 'comm': (task / 'comm').read_text().strip(),
                                                       'sched_runtime_ns': sched[0], 'sched_wait_ns': sched[1],
                                                       'sched_slices': sched[2]}
            except FileNotFoundError:
                pass  # A thread exiting between directory enumeration and read.
    data['collection_ns'] = time.monotonic_ns() - began
    return data


class SchedulingMonitor:
    def __init__(self, path, *, interval=.1, max_seconds=180):
        if not .05 <= interval <= 1 or not 1 <= max_seconds <= 600:
            raise ValueError('Use bounded sampling limits')
        self.path, self.interval, self.max_seconds = Path(path), interval, max_seconds
        self.stop = threading.Event()
        self.error = None
        self.group = cgroup_path()

    def __enter__(self):
        self.stream = self.path.open('x', buffering=1024 * 1024)
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        return self

    def run(self):
        try:
            while not self.stop.is_set() and time.monotonic() - self.started <= self.max_seconds:
                self.stream.write(json.dumps(snapshot(self.group)) + '\n')
                self.stop.wait(self.interval)
        except BaseException as error:
            self.error = repr(error)

    def __exit__(self, kind, value, traceback):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('Scheduling monitor did not stop')
        self.stream.close()
        if self.error:
            raise RuntimeError('Incomplete scheduling evidence: ' + self.error)


def trace_summary(payload):
    if payload.get('method') != 'monotonic-send-stages-v1' or payload.get('diagnostic_only') is not True:
        raise ValueError('Unsupported sender trace')
    rows = payload['rows']
    if not rows or len(rows) > 120016:
        raise ValueError('Invalid trace length')
    previous = None
    largest = []
    maxima = {'late_wakeup_ns': 0, 'lock_wait_ns': 0, 'before_write_ns': 0, 'write_ns': 0, 'send_gap_ns': 0}
    for row in rows:
        values = [row[key] for key in ('woke_ns', 'lock_acquired_ns', 'write_start_ns', 'write_end_ns')]
        if any(not isinstance(value, int) or value < 0 for value in values) or values != sorted(values):
            raise ValueError('Sender trace times are not monotonic')
        stages = {'late_wakeup_ns': max(0, row['woke_ns'] - row['planned_ns']),
                  'lock_wait_ns': row['lock_acquired_ns'] - row['woke_ns'],
                  'before_write_ns': row['write_start_ns'] - row['lock_acquired_ns'],
                  'write_ns': row['write_end_ns'] - row['write_start_ns'],
                  'send_gap_ns': row['woke_ns'] - previous if previous is not None else 0}
        if stages['send_gap_ns'] < 0:
            raise ValueError('Sender rows are out of order')
        previous = row['woke_ns']
        maxima = {key: max(maxima[key], stages[key]) for key in maxima}
        largest.append({**row, **stages})
    largest.sort(key=lambda row: row['send_gap_ns'], reverse=True)
    return {'diagnostic_only': True, 'rows': len(rows), 'maxima': maxima, 'largest_send_gaps': largest[:10],
            'gc_cycles': payload['gc_cycles'], 'gc_pause_total_ns': payload['gc_pause_total_ns'],
            'gc_recent_pauses': payload['gc_recent_pauses']}
