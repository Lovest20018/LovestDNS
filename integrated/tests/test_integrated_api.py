from pathlib import Path
import struct

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
