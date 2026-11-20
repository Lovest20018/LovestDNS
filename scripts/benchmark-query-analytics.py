"""Measure query summary latency and write backlog with a disposable, full database."""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'integrated'))

from runtime.query_recorder import Recorder


def measure(root):
    recorder = Recorder(root, root)
    observed = int(time.time()) - 90
    event = {'domain': 'capacity.example.test.', 'elapsed_ms': .7, 'qtype': 1, 'source_ip': '192.0.2.20',
             'outcome': 'response_observed', 'rcode': 'NOERROR', 'addresses': ['192.0.2.1'],
             'timing_scope': 'passive_entry_packet_interval', 'context': 'fixture padding ' * 5}
    for start in range(0, 104_000, 128):
        recorder.store.append([{**event, 'id': str(start + row), 'observed_at': observed + row / 10000,
                                'domain': f'{row}.capacity.example.test.', 'source_ip': f'192.0.2.{row % 254 + 1}'}
                               for row in range(128)])

    recorder.configure(True)
    writer = threading.Thread(target=recorder.write_loop)
    finished = threading.Event()
    journal = recorder.store.path.with_name('events.sqlite-journal')
    peak_pending = 0
    peak_disk = recorder.store.path.stat().st_size

    def produce():
        nonlocal peak_pending, peak_disk
        while not finished.is_set():
            now = time.time()
            for i in range(16):
                recorder.emit(recorder.generation, {**event, 'observed_at': now, 'id': f'{now}:{i}'})
            peak_pending = max(peak_pending, recorder.events.qsize())
            try:
                journal_bytes = journal.stat().st_size
            except FileNotFoundError:
                journal_bytes = 0
            peak_disk = max(peak_disk, recorder.store.path.stat().st_size + journal_bytes)
            finished.wait(.04)

    producer = threading.Thread(target=produce)
    writer.start()
    producer.start()
    samples = []
    try:
        for _ in range(12):
            start = time.perf_counter()
            result = recorder.request({'operation': 'analytics', 'hours': 24})
            samples.append(round((time.perf_counter() - start) * 1000, 2))
            assert result['total'] == sum(row['count'] for row in result['buckets'])
            time.sleep(.1)
    finally:
        finished.set()
        producer.join()
        deadline = time.monotonic() + 5
        while not recorder.events.empty() and time.monotonic() < deadline:
            time.sleep(.025)
        recorder.stop.set()
        writer.join()
    state = recorder.store.summary()
    return {'summary_ms': samples, 'median_ms': statistics.median(samples), 'max_ms': max(samples),
            'rows': state['retained_rows'], 'database_bytes': state['database_bytes'], 'peak_storage_bytes': peak_disk,
            'peak_pending': peak_pending, 'pending_after_drain': recorder.events.qsize(), 'counters': dict(recorder.counters)}


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='lovestdns-query-analytics-') as temp:
        print(json.dumps(measure(Path(temp)), sort_keys=True))
