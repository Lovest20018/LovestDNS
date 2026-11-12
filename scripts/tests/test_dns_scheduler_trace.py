"""Check target filtering, clock bounds and conservative scheduling attribution."""

import json
from pathlib import Path
import runpy
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from dns_capture import WIRE_RECORD
from dns_scheduler_trace import receiver_events

analyze = runpy.run_path(str(SCRIPTS / 'analyze-dns-receiver.py'))['analyze']


def test_receiver_filter_retains_cross_cpu_wakeup_and_strips_other_task_identity():
    raw = '''# tracer: nop
       secret-server-4 [011] d..2. 10.000001: sched_wakeup: comm=perf-recv-0000 pid=42 prio=120 target_cpu=013
       secret-worker-5 [013] d..2. 10.000010: sched_switch: prev_comm=other prev_pid=5 prev_prio=120 prev_state=R ==> next_comm=perf-recv-0000 next_pid=42 next_prio=120
       perf-recv-0000-42 [013] d..2. 10.001000: sched_switch: prev_comm=perf-recv-0000 prev_pid=42 prev_prio=120 prev_state=R+ ==> next_comm=other next_pid=5 next_prio=120
'''
    assert receiver_events(raw, 42) == [(10_000_001_000, 11, 'wake', '-'),
                                        (10_000_010_000, 13, 'in', '-'),
                                        (10_001_000_000, 13, 'out', 'R+')]
    with pytest.raises(ValueError, match='unrelated'):
        receiver_events(raw, 41)
    with pytest.raises(ValueError, match='format'):
        receiver_events('LOST EVENTS', 42)


def evidence(tmp_path, events):
    prefix = tmp_path / 'dnsperf.scheduler'
    # Realtime is monotonic + 1 second, sampled in a 100ns bracket.
    metadata = {'receiver_tid': 42, 'events': len(events), 'clocks': [[999_999_950, 2_000_000_000, 1_000_000_050]],
                'global_before': {'tracing_on': '1'}, 'global_after': {'tracing_on': '1'}}
    Path(str(prefix) + '.json').write_text(json.dumps(metadata))
    Path(str(prefix) + '.events.tsv').write_text('mono_ns cpu event prev_state\n' +
        ''.join(f'{t} {cpu} {kind} {state}\n' for t, cpu, kind, state in events))
    (tmp_path / 'dnsperf.client.tsv').write_text('id sent_us read_us rcode\n1 2000100 2004000 0\n')
    (tmp_path / 'dnsperf.wire.bin').write_bytes(WIRE_RECORD.pack(1, 2_000_200_000, 2_001_000_000))
    return prefix, metadata


@pytest.mark.parametrize('kind,state,majority', [('out', 'R+', 'runnable'), ('out', 'S', 'sleeping'), ('in', '-', 'running')])
def test_post_reply_state_is_classified_from_same_query_clock_interval(tmp_path, kind, state, majority):
    prefix, _ = evidence(tmp_path, [(1_000_000_000, 13, kind, state), (1_005_000_000, 13, 'out', 'S')])
    result = analyze(prefix)
    assert result['covered']['majority'] == {majority: 1}
    assert result['covered']['median_us'][majority] == 3000
    assert result['clock_uncertainty_ns'] == 2050
    assert result['post_reply_at_least_1ms']['samples'] == 1
    assert result['performance_accepted'] is False


def test_equal_printed_timestamps_and_startup_remain_unknown(tmp_path):
    prefix, _ = evidence(tmp_path, [(1_000_000_000, 13, 'in', '-'),
                                   (1_000_000_000, 14, 'out', 'R'), (1_005_000_000, 14, 'in', '-')])
    result = analyze(prefix)
    assert result['ambiguous_timestamp_groups'] == 1
    assert result['covered']['majority'] == {'mixed_or_short': 1}
    assert result['covered']['median_us']['unknown'] == 3000
    prefix, _ = evidence(tmp_path, [(1_002_000_000, 13, 'in', '-'), (1_005_000_000, 13, 'out', 'S')])
    assert analyze(prefix)['uncovered_packet_samples'] == 1


def test_clock_step_or_global_change_cannot_produce_attribution(tmp_path):
    prefix, meta = evidence(tmp_path, [(1_000_000_000, 13, 'in', '-'), (1_005_000_000, 13, 'out', 'S')])
    meta['clocks'].append([2_000_000_000, 3_001_000_000, 2_000_000_100])
    Path(str(prefix) + '.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='uncertainty'):
        analyze(prefix)
    meta['global_after']['tracing_on'] = '0'
    Path(str(prefix) + '.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='global'):
        analyze(prefix)
