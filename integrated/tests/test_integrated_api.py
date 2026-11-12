from pathlib import Path
import json
import struct
import sqlite3

import pytest
import server
from history import History

AUTH = {"Authorization": "Bearer integrated-test"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(server.base, "DATA_DIR", str(data))
    monkeypatch.setattr(server.base, "WEB_UI_TOKEN", "integrated-test")
    monkeypatch.setattr(server.base, "WEB_UI_ALLOW_NO_AUTH", False)
    monkeypatch.setattr(server, "history", History(state))
    monkeypatch.setattr(server, "STATE_DIR", state)
    monkeypatch.setattr(server, "RUN_DIR", tmp_path / "run")
    from browser_auth import set_external_api
    set_external_api(state, True)  # Existing API tests explicitly opt in.
    server.app.config["TESTING"] = True
    with server.app.test_client() as value:
        yield value, data, state


@pytest.mark.parametrize("route", ["/api/capabilities", "/api/runtime", "/api/history/custom_env.ini", "/api/core-logs/redis"])
def test_integrated_routes_require_auth(client, route):
    assert client[0].get(route).status_code == 401


def test_missing_runtime_disables_runtime_feature(client, monkeypatch):
    monkeypatch.setattr(server.runtime_client, "snapshot", lambda: {"available": False, "error": "missing"})
    result = client[0].get("/api/capabilities", headers=AUTH).get_json()
    assert result["edition"] == "integrated"
    assert result["features"]["runtime_snapshot"] is False
    assert result["features"]["config_apply"] is False
    assert client[0].get("/api/runtime", headers=AUTH).status_code == 503


def test_worker_degradation_is_visible_without_marking_dns_failed(client, monkeypatch):
    monkeypatch.setattr(server.runtime_client, 'manager_request', lambda *args: {'apply_custom_mod': False, 'save_redis_snapshot': False, 'restart_dns_core': False, 'worker': {'available': False, 'phase': 'result_unconfirmed'}})
    value = client[0].get('/api/v1/capabilities', headers=AUTH).get_json()['data']
    assert value['maintenance'] == {'available': False, 'phase': 'result_unconfirmed'}
    assert value['features']['redis_snapshot'] is False


def test_status_keeps_saved_overrides_distinct_from_running_environment(client):
    api, data, _ = client
    server.RUN_DIR.mkdir()
    (server.RUN_DIR / 'core-env.json').write_text(json.dumps({'CNAUTO': 'yes', 'IPV6': 'raw', 'UPDATE': 'weekly', 'PRIVATE_TOKEN': 'never-expose'}))
    (data / 'custom_env.ini').write_text('CNAUTO="no"\nIPV6="no"\n')
    status = api.get('/api/status', headers=AUTH).get_json()
    assert status['env'] == {'CNAUTO': 'yes', 'IPV6': 'raw', 'UPDATE': 'weekly'}
    assert api.get('/api/env', headers=AUTH).get_json()['CNAUTO'] == 'no'
    result = api.put('/api/file/custom_env.ini', headers=AUTH, json={'content': 'CNAUTO="no"\n'}).get_json()
    assert result['requires_restart'] is True and result['verified'] is False
    assert result['watched_now'] is False


def test_bootstrap_logs_redact_sensitive_values_from_captured_runtime(client):
    api, _, state = client
    server.RUN_DIR.mkdir()
    (server.RUN_DIR / 'core-env.json').write_text('{"CUSTOM_TOKEN":"core-secret-value"}')
    (state / 'logs').mkdir(parents=True)
    (state / 'logs/bootstrap.log').write_text('initializing core-secret-value\n')
    result = api.get('/api/core-logs/bootstrap', headers=AUTH).get_json()
    assert result['lines'] == ['initializing ***']


def test_history_records_original_save_and_validated_restore(client):
    api, data, state = client
    original = 'CNAUTO="yes"\n'
    (data / "custom_env.ini").write_text(original)
    result = api.put("/api/file/custom_env.ini", json={"content": 'CNAUTO="no"\n'}, headers=AUTH)
    assert result.status_code == 200
    revisions = api.get("/api/history/custom_env.ini", headers=AUTH).get_json()["revisions"]
    assert len(revisions) == 2
    first = revisions[-1]["id"]
    restored = api.post(f"/api/history/custom_env.ini/{first}/restore", headers=AUTH)
    assert restored.status_code == 200
    assert (data / "custom_env.ini").read_text() == original
    assert (state / "config-history.sqlite").exists()
    assert not (data / "config-history.sqlite").exists()
    assert restored.get_json()["verified"] is False


def test_restore_revalidates_legacy_content(client):
    api, data, _ = client
    (data / "custom_env.ini").write_text("invalid old format")
    assert api.put("/api/file/custom_env.ini", json={"content": 'CNAUTO="yes"\n'}, headers=AUTH).status_code == 200
    revision = api.get("/api/history/custom_env.ini", headers=AUTH).get_json()["revisions"][-1]["id"]
    assert api.post(f"/api/history/custom_env.ini/{revision}/restore", headers=AUTH).status_code == 400
    assert (data / "custom_env.ini").read_text() == 'CNAUTO="yes"\n'


def test_history_is_bounded_and_filename_scoped(client):
    api, _, _ = client
    for i in range(25):
        assert api.put("/api/file/custom_env.ini", json={"content": f'VALUE="{i}"\n'}, headers=AUTH).status_code == 200
    revisions = api.get("/api/history/custom_env.ini", headers=AUTH).get_json()["revisions"]
    assert len(revisions) == 20
    assert api.get(f'/api/history/custom_mod.yaml/{revisions[0]["id"]}', headers=AUTH).status_code == 404
    assert api.get("/api/history/redis_dns_v2.rdb", headers=AUTH).status_code == 403


def test_history_failure_prevents_configuration_write(client, monkeypatch):
    api, data, _ = client
    target = data / "custom_env.ini"
    target.write_text('VALUE="old"\n')
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(server.history, "connection", fail)
    response = api.put("/api/file/custom_env.ini", json={"content": 'VALUE="new"\n'}, headers=AUTH)
    assert response.status_code == 500
    assert target.read_text() == 'VALUE="old"\n'


@pytest.mark.parametrize("stage", ["record", "prune", "commit"])
def test_post_write_failure_keeps_durable_recovery(client, monkeypatch, stage):
    from contextlib import contextmanager
    api, data, state = client
    target = data / "custom_env.ini"
    target.write_bytes(b'VALUE="old"\n')
    original = getattr(server.history, "write_lock" if stage == "commit" else stage)
    if stage == "commit":
        calls = 0
        @contextmanager
        def failing_commit():
            nonlocal calls
            calls += 1
            with original() as db:
                yield db
                if calls == 3:  # recovery, intent commit, final commit
                    raise sqlite3.OperationalError("commit failed")
        monkeypatch.setattr(server.history, "write_lock", failing_commit)
    else:
        def fail(*args):
            raise sqlite3.OperationalError("history full")
        monkeypatch.setattr(server.history, stage, fail)
    response = api.put("/api/file/custom_env.ini", json={"content": 'VALUE="new"\n'}, headers=AUTH)
    assert response.status_code == 503
    assert target.read_bytes() == b'VALUE="new"\n'
    with sqlite3.connect(state / "config-history.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM write_intents").fetchone()[0] == 1
    monkeypatch.setattr(server.history, "write_lock" if stage == "commit" else stage, original)
    # Reopening history reconciles the persisted file without replaying a write.
    revisions = api.get("/api/history/custom_env.ini", headers=AUTH).get_json()["revisions"]
    assert len(revisions) == 2
    assert target.read_bytes() == b'VALUE="new"\n'


def test_interrupted_write_preserves_external_edit_and_candidate(client):
    api, data, _ = client
    target = data / "custom_env.ini"
    server.history.prepare_write("custom_env.ini", b"", b'VALUE="candidate"\n')
    target.write_bytes(b'VALUE="external"\n')
    revisions = api.get("/api/history/custom_env.ini", headers=AUTH).get_json()["revisions"]
    assert {r["reason"] for r in revisions} == {"before_save", "unconfirmed_candidate", "recovered_external"}
    assert target.read_bytes() == b'VALUE="external"\n'


def test_external_reversion_after_save_keeps_unconfirmed_candidate(client, monkeypatch):
    api, data, _ = client
    target = data / "custom_env.ini"
    original, candidate = b'VALUE="old"\n', 'VALUE="candidate"\n'
    target.write_bytes(original)
    write = server._original_write

    def write_then_revert(filename, content):
        result = write(filename, content)
        assert result[0]
        target.write_bytes(original)
        return result

    monkeypatch.setattr(server, '_original_write', write_then_revert)
    revision = api.get('/api/v1/file/custom_env.ini', headers=AUTH).get_json()['data']['revision']
    response = api.put('/api/v1/file/custom_env.ini', headers=AUTH,
                       json={'content': candidate, 'expected_revision': revision})
    assert response.status_code == 503
    assert response.get_json()['error']['code'] == 'SAVE_UNCONFIRMED'
    assert target.read_bytes() == original
    revisions = api.get('/api/v1/history/custom_env.ini', headers=AUTH).get_json()['data']['revisions']
    pending = next(row for row in revisions if row['reason'] == 'unconfirmed_candidate')
    recovered = api.get(f'/api/v1/history/custom_env.ini/{pending["id"]}', headers=AUTH).get_json()['data']
    assert recovered['content'] == candidate
    assert not server.history.has_pending('custom_env.ini')


@pytest.mark.parametrize('original', [None, b'', b'VALUE="old"\n'])
def test_lazy_recovery_retains_candidate_when_disk_matches_original(client, original):
    api, data, _ = client
    target = data / 'custom_env.ini'
    if original is not None:
        target.write_bytes(original)
    candidate = b'VALUE="candidate"\n'
    server.history.prepare_write('custom_env.ini', original, candidate)
    revisions = api.get('/api/v1/history/custom_env.ini', headers=AUTH).get_json()['data']['revisions']
    pending = next(row for row in revisions if row['reason'] == 'unconfirmed_candidate')
    assert server.history.read('custom_env.ini', pending['id']) == candidate.decode()
    assert (target.read_bytes() if target.exists() else None) == original
    assert not server.history.has_pending('custom_env.ini')


def test_failed_write_restores_empty_original(client):
    target = client[1] / "custom_env.ini"
    target.write_bytes(b"partial")
    server.base._restore_failed_write(str(target), True, b"", True)
    assert target.read_bytes() == b""


def test_v1_unconfirmed_save_has_distinct_error_code(client, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError('full')
    monkeypatch.setattr(server.history, 'prune', fail)
    response = client[0].put('/api/v1/file/custom_env.ini', json={'content': 'VALUE="new"\n'}, headers=AUTH)
    assert response.status_code == 503
    assert response.get_json()['error']['code'] == 'SAVE_UNCONFIRMED'
    assert response.headers['X-Request-ID']


def test_legacy_file_over_limit_is_not_silently_truncated(client):
    api, data, _ = client
    original = b"#" * (256 * 1024 + 1)
    (data / "custom_env.ini").write_bytes(original)
    assert api.put("/api/file/custom_env.ini", json={"content": 'VALUE="new"\n'}, headers=AUTH).status_code == 500
    assert (data / "custom_env.ini").read_bytes() == original


def test_startup_conditions_use_captured_environment(client, monkeypatch):
    monkeypatch.setattr(server, "boot_env", lambda: {"CNAUTO": "no"})
    response = client[0].put("/api/file/custom_env.ini", json={"content": 'CNAUTO="yes"\n'}, headers=AUTH)
    assert response.get_json()["watched_now"] is False


def test_nonstandard_recursive_port_is_discovered(client, monkeypatch):
    monkeypatch.setattr(server, "boot_env", lambda: {"CNAUTO": "no", "DNSPORT": "5353"})
    result = client[0].get("/api/capabilities", headers=AUTH).get_json()
    assert result["dns_defaults"]["port"] == 5353


def test_logs_bounded_and_token_redacted(client, monkeypatch):
    api, _, state = client
    (state / "logs").mkdir(parents=True)
    (state / "logs/redis.log").write_text("line\n" * 500 + "token=secret-test\n")
    monkeypatch.setenv("WEB_UI_TOKEN", "secret-test")
    result = api.get("/api/core-logs/redis", headers=AUTH).get_json()
    assert len(result["lines"]) == 200
    assert result["lines"][-1] == "token=***"
    assert api.get("/api/core-logs/other", headers=AUTH).status_code == 404


def test_dns_api_includes_debug_without_changing_rcode(client, monkeypatch):
    def exchange(query, *args):
        text = b"From: recursive"
        extra = server.base._encode_dns_name("1.addinfo.paopaodns") + struct.pack("!HHIH", 16, 1, 0, len(text) + 1) + bytes([len(text)]) + text
        return struct.pack("!HHHHHH", int.from_bytes(query[:2], "big"), 0x8183, 1, 0, 0, 1) + query[12:] + extra
    monkeypatch.setattr(server.base, "_exchange_dns", exchange)
    result = client[0].post("/api/dns-test", json={"domain": "example.com", "record_type": "TXT"}, headers=AUTH).get_json()
    assert result["rcode"] == "NXDOMAIN"
    assert result["available"] is False
    assert result["debug"][0]["value"] == "From: recursive"
