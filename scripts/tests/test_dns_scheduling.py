"""Protect scheduling attribution against malformed or misleading trace times."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dns_scheduling import trace_summary


def evidence():
    return {'method': 'monotonic-send-stages-v1', 'diagnostic_only': True,
            'gc_cycles': 0, 'gc_pause_total_ns': 0, 'gc_recent_pauses': [],
            'rows': [{'index': 0, 'planned_ns': 0, 'woke_ns': 1, 'lock_acquired_ns': 2,
                      'write_start_ns': 3, 'write_end_ns': 4},
                     {'index': 1, 'planned_ns': 10_000_000, 'woke_ns': 197_000_001,
                      'lock_acquired_ns': 197_000_003, 'write_start_ns': 197_000_004, 'write_end_ns': 197_000_006}]}


def test_late_wakeup_is_separated_from_lock_and_send_call():
    result = trace_summary(evidence())
    assert result['maxima']['send_gap_ns'] == 197_000_000
    assert result['maxima']['lock_wait_ns'] == result['maxima']['write_ns'] == 2
    assert result['largest_send_gaps'][0]['index'] == 1
    assert result['diagnostic_only'] is True


@pytest.mark.parametrize('change', ['backwards', 'negative', 'reordered', 'wrong_method', 'not_diagnostic', 'empty'])
def test_invalid_traces_cannot_be_used_for_attribution(change):
    data = evidence()
    if change == 'backwards': data['rows'][1]['write_end_ns'] = 0
    elif change == 'negative': data['rows'][0]['woke_ns'] = -1
    elif change == 'reordered': data['rows'].reverse()
    elif change == 'wrong_method': data['method'] = 'unknown'
    elif change == 'not_diagnostic': data['diagnostic_only'] = False
    else: data['rows'] = []
    with pytest.raises(ValueError):
        trace_summary(data)
