import json
import time
import pytest
import server
from api_v1 import metric_values
from api_schema import openapi_document
from test_integrated_api import client, AUTH

READ = {"Authorization": "Bearer external-observer"}


@pytest.fixture(autouse=True)
def observer(monkeypatch):
    monkeypatch.setenv("SUITE_READ_TOKEN", "external-observer")


def test_instance_is_stable_across_requests_and_state_isolation(client, monkeypatch, tmp_path):
    api, _, state = client
    first = api.get("/api/v1/instance", headers=READ)
    assert first.status_code == 200
    identity = first.get_json()["data"]["id"]
    assert (state / "instance.sqlite").exists()
    assert api.get("/api/v1/instance", headers=AUTH).get_json()["data"]["id"] == identity
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "another-state")
    assert api.get('/api/v1/instance', headers=READ).status_code == 403
    from browser_auth import set_external_api
    set_external_api(server.STATE_DIR, True)
    assert api.get("/api/v1/instance", headers=READ).get_json()["data"]["id"] != identity


@pytest.mark.parametrize("path,method", [("/status", "get"), ("/env", "get"), ("/file/custom_env.ini", "get"),
    ("/file/custom_env.ini", "put"), ("/config/apply-plan", "get"), ("/maintenance/tasks", "get"),
    ("/maintenance/tasks", "post"), ("/history/custom_env.ini", "get"),
    ("/history/custom_env.ini/1", "get"), ("/history/custom_env.ini/1/restore", "post"), ("/core-logs/redis", "get")])
def test_observer_cannot_read_sensitive_resources_or_write(client, path, method):
    response = getattr(client[0], method)("/api/v1" + path, headers=READ, json={"content": 'VALUE="bad"\n'})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "FORBIDDEN"


def test_observer_cannot_authorize_legacy_endpoints(client):
    for path in ("/api/status", "/api/file/custom_env.ini", "/api/capabilities"):
        assert client[0].get(path, headers=READ).status_code == 401


def test_auth_unknown_route_and_request_tracing(client):
    api = client[0]
    response = api.get("/api/v1/instance")
    assert response.status_code == 401
    assert response.get_json()["meta"]["request_id"] == response.headers["X-Request-ID"]
    response = api.get("/api/v1/instance", headers={**AUTH, "X-Request-ID": "external-check-123"})
    assert response.get_json()["meta"] == {"api_version": "1", "request_id": "external-check-123"}
    assert response.headers["Cache-Control"] == "no-store"
    assert api.get("/api/v1/not-a-route", headers=AUTH).get_json()["error"]["code"] == "NOT_FOUND"


def test_invalid_json_and_wrong_content_type_use_envelope(client):
    api = client[0]
    response = api.put("/api/v1/file/custom_env.ini", headers={**AUTH, "Content-Type": "application/json"}, data="{")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_ARGUMENT"
    response = api.put("/api/v1/file/custom_env.ini", headers=AUTH, data="content=text")
    assert response.status_code == 415
    assert response.get_json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_new_and_legacy_config_routes_share_history_and_validation(client):
    api, data, _ = client
    result = api.put("/api/v1/file/custom_env.ini", headers=AUTH, json={"content": 'VALUE="v1"\n'})
    assert result.status_code == 200
    assert result.get_json()["data"]["verified"] is False
    assert api.get("/api/file/custom_env.ini", headers=AUTH).get_json()["content"] == 'VALUE="v1"\n'
    revision = api.get("/api/v1/history/custom_env.ini", headers=AUTH).get_json()["data"]["revisions"][0]
    assert revision["reason"] == "saved"
    invalid = api.put("/api/v1/file/custom_env.ini", headers=AUTH, json={"content": "bad-format"})
    assert invalid.status_code == 400
    assert (data / "custom_env.ini").read_text() == 'VALUE="v1"\n'


def test_capabilities_are_permission_aware_and_missing_runtime_has_no_zeroes(client, monkeypatch):
    monkeypatch.setattr(server.runtime_client, "snapshot", lambda: {"available": False, "error": "observer offline"})
    monkeypatch.setattr(server.runtime_client, "manager_request", lambda *args: {"apply_custom_mod": True, "save_redis_snapshot": True})
    value = client[0].get("/api/v1/capabilities", headers=READ).get_json()["data"]
    assert value["access_role"] == "observer"
    assert not value["permissions"]["configure"]
    assert not value["features"]["config_apply"]
    response = client[0].get("/api/v1/metrics", headers=READ)
    assert response.status_code == 503
    assert "data" not in response.get_json()


def test_metrics_identify_source_and_do_not_invent_global_values(client, monkeypatch):
    now = time.time()
    sample = {"schema_version": 1, "sampled_at": now, "interval_seconds": 5,
              "layers": {"recursive": {"available": True, "comparable": True, "qps": 4.2, "cache_hit_ratio": .8},
                         "forward": {"available": False}}, "redis": {"available": True, "info": {"used_memory": 1048576}}}
    monkeypatch.setattr(server.runtime_client, "snapshot", lambda: sample)
    result = client[0].get("/api/v1/metrics", headers=READ).get_json()["data"]
    assert result["sampled_at"] == now
    assert result["metrics"][0] == {"name": "qps", "source": "unbound", "layer": "recursive", "unit": "queries/second", "value": 4.2, "available": True,
                                   "window_seconds": None, "unavailable_reason": None}
    assert result["metrics"][2]["value"] is None
    assert len(result["metrics"]) == 5
    sample["layers"]["recursive"]["qps"] = float("nan")
    assert metric_values(sample)["metrics"][0]["value"] is None


def test_observer_can_query_but_cannot_submit_tasks(client, monkeypatch):
    calls = []
    def lookup(*args):
        calls.append(args)
        return {"available": False, "rcode": "NXDOMAIN", "status": "dns_error", "answers": [], "results": []}
    monkeypatch.setattr(server.base, "_dns_lookup", lookup)
    response = client[0].post("/api/v1/dns-test", headers=READ, json={"domain": "missing.example", "record_type": "TXT"})
    assert response.status_code == 200
    assert response.get_json()["data"]["rcode"] == "NXDOMAIN"
    assert calls[0][1] == "TXT"


def test_spec_routes_match_and_are_tool_consumable(client):
    response = client[0].get("/api/v1/openapi.json", headers=READ)
    doc = response.get_json()
    assert doc["openapi"] == "3.1.0"
    assert "data" not in doc
    actual = {(str(rule).replace("/api/v1", ""), method) for rule in server.app.url_map.iter_rules()
              if rule.endpoint.startswith("v1.") for method in rule.methods - {"HEAD", "OPTIONS"}}
    import re
    normalized = {(re.sub(r"<(?:int:)?(\w+)>", r"{\1}", route), method.lower()) for route, method in actual}
    assert normalized == {(route, method) for route, methods in doc["paths"].items() for method in methods}
    json.dumps(openapi_document("test"), allow_nan=False)
