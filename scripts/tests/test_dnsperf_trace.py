from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from dnsperf_trace import read_thread_affinity, split_timings, timing_summary


def test_reused_id_is_joined_by_time_and_long_client_delay_is_attributed():
    client = [(17, 10000, 14000, 0), (17, 20000, 20120, 0)]
    wire = [(17, 20010500, 20090400), (17, 10010000, 10200000)]
    timing = timing_summary(split_timings(client, wire))
    assert timing['samples'] == 2
    assert timing['client']['p99_ms'] == 4
    assert timing['kernel_roundtrip']['p99_ms'] == .19
    assert timing['after_kernel_reply']['p99_ms'] == 3.8
    assert timing['slowest_client_one_percent']['post_reply_largest'] == 1


@pytest.mark.parametrize('wire', [
    [(17, 10010000, 10200000)],  # Missing reused ID.
    [(17, 9999999, 10200000), (17, 20010000, 20090000)],  # Query before client timestamp.
    [(17, 10010000, 14001000), (17, 20010000, 20090000)],  # Reply after client read.
    [(17, 10010000, 10200000), (18, 20010000, 20090000)],  # Wrong ID.
])
def test_incomplete_or_unprovable_timings_do_not_produce_tail_statistics(wire):
    with pytest.raises(ValueError, match='differ|ordering'):
        split_timings([(17, 10000, 14000, 0), (17, 20000, 20120, 0)], wire)


def test_streaming_log_and_deferred_trace_use_identical_client_intervals():
    parser = runpy.run_path(str(SCRIPTS / 'benchmark-dns.py'))['parse_dnsperf']
    footer = ('Queries sent: 2\nQueries completed: 2\nQueries lost: 0\n'
              'Queries per second: 2.0\nRun time (s): 1.0\n')
    live = '> NOERROR hot.cache.test A 0.000120\n> NOERROR hot.cache.test A 0.004000\n'
    client = [(1, 20000, 20120, 0), (0, 10000, 14000, 0)]
    assert parser(live + footer, client, 'stream') == parser(footer, client, 'memory')
    with pytest.raises(ValueError, match='live log differs'):
        parser(live.replace('0.004000', '0.004100') + footer, client, 'stream')
    with pytest.raises(ValueError, match='unexpectedly produced'):
        parser(live + footer, client, 'memory')


def test_thread_affinity_is_proven_before_the_first_query(tmp_path):
    path = tmp_path / 'dnsperf.affinity.tsv'
    path.write_text('role cpu verified_us start_us\nreceiver 13 100 110\nsender 14 105 110\n')
    record = read_thread_affinity(path, {'receiver': 13, 'sender': 14}, [(1, 112, 130, 0)])
    assert record['complete'] and record['records']['sender']['verified_us'] == 105


@pytest.mark.parametrize('rows', [
    'receiver 13 100 110\n',
    'receiver 13 100 110\nreceiver 14 105 110\n',
    'receiver 14 100 110\nsender 13 105 110\n',
    'receiver 13 115 110\nsender 14 105 110\n',
    'receiver 13 100 115\nsender 14 105 115\n',
    'receiver 13 100 110\nsender 14 105 111\n',
])
def test_missing_wrong_cpu_or_late_thread_placement_invalidates_evidence(tmp_path, rows):
    path = tmp_path / 'dnsperf.affinity.tsv'
    path.write_text('role cpu verified_us start_us\n' + rows)
    with pytest.raises(ValueError):
        read_thread_affinity(path, {'receiver': 13, 'sender': 14}, [(1, 112, 130, 0)])
