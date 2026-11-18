from io import BytesIO
import math
import pytest
import runtime_client
from runtime_client import sample_age


@pytest.mark.parametrize('reply,kind,text', [
    (b'', RuntimeError, '响应无效'),
    (b'{"ok":\n', RuntimeError, '响应无效'),
    (b'{"ok":true}\n', RuntimeError, '响应无效'),
    (b'{"ok":false,"code":"UNKNOWN"}\n', RuntimeError, '响应无效'),
    (b'{"ok":false,"code":"CONFLICT","error":"rejected"}\n', ValueError, 'rejected'),
])
def test_task_submission_distinguishes_unknown_result_from_conflict(monkeypatch, reply, kind, text):
    sent = []

    class MockSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, _): pass
        def connect(self, _): pass
        def sendall(self, request): sent.append(request)
        def makefile(self, _): return BytesIO(reply)

    monkeypatch.setattr(runtime_client.socket, 'AF_UNIX', 1, raising=False)
    monkeypatch.setattr(runtime_client.socket, 'socket', lambda *_: MockSocket())
    with pytest.raises(kind, match=text):
        runtime_client.manager_request('submit', {'request_id': 'a' * 32})
    assert len(sent) == 1 and b'"operation": "submit"' in sent[0]


@pytest.mark.parametrize('sampled', [None, True, '123', math.nan, math.inf, -math.inf])
def test_invalid_sample_timestamp_is_never_current(sampled):
    with pytest.raises(ValueError):
        sample_age({'sampled_at': sampled}, now=100)


def test_sample_age_has_explicit_boundary_and_rejects_clock_skew():
    assert sample_age({'sampled_at': 75}, now=100) == 25
    assert sample_age({'sampled_at': 100}, now=100) == 0
    for sampled in (74.9, 101):
        with pytest.raises(ValueError):
            sample_age({'sampled_at': sampled}, now=100)
