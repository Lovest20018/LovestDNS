"""Consumer-facing frozen fixtures must stay tied to actual provider behavior."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from jsonschema import ValidationError
import pytest

import server
from api_v1 import READ_OPERATIONS, instance_identity
from test_integrated_api import client  # noqa: F401 - shared fixture

ROOT = Path(__file__).resolve().parents[2]
EXPORTER = ROOT / "scripts/export-api-contract.py"
BASELINE = ROOT / "docs/contracts/v1"
export = runpy.run_path(str(EXPORTER))
READ = {"Authorization": "Bearer provider-test-observer", "X-Request-ID": "provider-test-0001"}


def test_source_provenance_covers_build_inputs_and_hashes_binary_bytes_exactly(tmp_path):
    fixture = tmp_path / 'source'
    files = {
        'scripts/browser.mjs': b'const value = 1;\r\n',
        'web/eslint.config.js': b'export default [];\r\n',
        'packaging/native/requirements.txt': b'Flask==3.1.1\r\n',
        'integrated/frontend/public/icon.png': b'\x89PNG\r\n\x1a\nbytes\r\n',
        'web/node_modules/ignored.js': b'ignored',
        'integrated/frontend/dist/ignored.png': b'ignored',
    }
    for name, data in files.items():
        path = fixture / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    summary = runpy.run_path(str(EXPORTER))['source_summary']
    summary.__globals__.update(ROOT=fixture, SOURCE_ROOT_FILES=())
    actual = summary()['files']
    expected = {name: hashlib.sha256(data if name.endswith('.png') else data.replace(b'\r\n', b'\n')).hexdigest()
                for name, data in files.items() if 'ignored' not in name}
    assert actual == expected


def test_committed_baseline_replays_and_export_is_independent_of_local_secrets(monkeypatch):
    monkeypatch.setenv("SUITE_INSTANCE_NAME", "must-not-leak")
    monkeypatch.setenv("WEB_UI_TOKEN", "must-not-leak")
    monkeypatch.setenv("SUITE_READ_TOKEN", "must-not-leak")
    monkeypatch.setenv("SUITE_BUILD_INFO", "/private/build-info.json")
    first = export["build_bundle"]()
    second = export["build_bundle"]()
    assert first == second
    for name, content in first.items():
        assert b"must-not-leak" not in content
        assert content == (BASELINE / name).read_bytes().replace(b"\r\n", b"\n"), name


def test_check_command_detects_drift_without_rewriting_files(tmp_path):
    for name in ("manifest.json", "openapi.json", "samples.json"):
        shutil.copyfile(BASELINE / name, tmp_path / name)
    path = tmp_path / "samples.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["cases"][0]["response"]["body"]["data"]["id"] = "changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, str(EXPORTER), "--check", "--output", str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and "samples.json" in result.stderr
    assert path.read_bytes() == before


def test_sample_config_revisions_hash_the_documented_utf8_bytes_on_every_host():
    samples = json.loads(export['build_bundle']()['samples.json'])
    cases = {case['id']: case for case in samples['cases']}
    expected = hashlib.sha256(cases['config_create']['request']['body']['content'].encode('utf-8')).hexdigest()
    for name in ('config_create', 'config_read'):
        assert cases[name]['response']['body']['data']['revision'] == expected
        assert cases[name]['response']['headers']['ETag'] == '"' + expected + '"'


def test_shared_samples_preserve_provider_semantics():
    bundle = export["build_bundle"]()
    samples = json.loads(bundle["samples.json"])
    cases = {case["id"]: case["response"] for case in samples["cases"]}
    identity = cases["observer_instance"]["body"]["data"]["id"]
    caps = cases["observer_capabilities"]["body"]["data"]
    assert caps["instance_id"] == identity
    assert caps["access_role"] == "observer"
    assert caps["permissions"] == dict(observe=True, diagnose=True, configure=False, maintain=False)
    assert not caps["features"]["config_apply"] and not caps["features"]["config_history"]
    assert not caps["features"]["redis_snapshot"]
    assert caps["dns_defaults"] == {"server": "127.0.0.1", "port": 53}
    assert not cases["capabilities_unavailable"]["body"]["data"]["features"]["runtime_snapshot"]
    runtime = cases["runtime_healthy"]["body"]["data"]
    assert 1_000_000_000 < runtime["sampled_at"] < 10_000_000_000  # seconds, not milliseconds
    assert runtime["freshness"] == {"age_seconds": 2.0, "max_age_seconds": 25}
    assert cases["runtime_partial"]["status"] == 200
    assert cases["runtime_partial"]["body"]["data"]["core_healthy"] is False
    for operation in ("runtime", "metrics"):
        for scenario, code in (("stale", "STALE_DATA"), ("unavailable", "UNAVAILABLE")):
            result = cases[operation + "_" + scenario]
            assert result["status"] == 503 and result["body"]["error"]["code"] == code
            assert "data" not in result["body"]
    values = cases["metrics_zero"]["body"]["data"]["metrics"]
    assert len(values) == 5
    for metric in values[:4]:
        assert metric["source"] == "unbound" and metric["window_seconds"] == 5
        if metric["name"] == "qps":
            assert metric["value"] == 0 and metric["available"] is True
            assert metric["unit"] == "queries/second"
        else:
            assert metric["value"] is None and metric["available"] is False
            assert metric["unavailable_reason"] == "no_queries" and metric["unit"] == "ratio"
    assert values[-1]["source"] == "redis" and values[-1]["unit"] == "bytes"
    for metric in cases["metrics_not_comparable"]["body"]["data"]["metrics"][:4]:
        assert metric["value"] is None and metric["window_seconds"] is None
        assert metric["unavailable_reason"] in {"process_changed", "first_sample"}
    for metric in cases["metrics_partial"]["body"]["data"]["metrics"][2:]:
        assert metric["value"] is None and metric["unavailable_reason"] == "component_unavailable"
    assert cases["unauthorized"]["status"] == 401
    assert cases["unauthorized"]["body"]["error"]["code"] == "UNAUTHORIZED"
    assert cases["forbidden_config"]["status"] == 403
    assert cases["forbidden_config"]["body"]["error"]["code"] == "FORBIDDEN"
    for key, available, status, rcode in (("dns_success", True, "ok", "NOERROR"),
                                           ("dns_nxdomain", False, "dns_error", "NXDOMAIN")):
        result = cases[key]
        assert result["status"] == 200
        dns = result["body"]["data"]
        assert (dns["available"], dns["responded"], dns["status"], dns["rcode"]) == (available, True, status, rcode)
        assert bool(dns["answers"]) == available
    assert cases["dns_compare_mixed"]["body"]["data"]["tests"][1]["result"]["rcode"] == "NXDOMAIN"
    assert cases["dns_health"]["body"]["data"]["pass"] is True


@pytest.mark.parametrize("broken", ["missing_id", "wrong_metric_type", "wrong_route", "wrong_request_id"])
def test_contract_validator_rejects_broken_shared_examples(broken):
    bundle = export["build_bundle"]()
    document, samples = (json.loads(bundle[name]) for name in ("openapi.json", "samples.json"))
    cases = {case["id"]: case for case in samples["cases"]}
    if broken == "missing_id":
        del cases["observer_instance"]["response"]["body"]["data"]["id"]
    elif broken == "wrong_metric_type":
        cases["metrics_zero"]["response"]["body"]["data"]["metrics"][0]["value"] = "0"
    elif broken == "wrong_route":
        cases["forbidden_config"]["request"]["path"] = "/api/configs"
    else:
        cases["unauthorized"]["response"]["headers"]["X-Request-ID"] = "different-request"
    with pytest.raises((ValidationError, ValueError)):
        export["validate_samples"](document, samples)


def test_machine_readable_roles_match_provider_allowlist():
    document = json.loads((BASELINE / "openapi.json").read_text(encoding="utf-8"))
    operations = [operation for methods in document["paths"].values() for operation in methods.values()]
    assert {op["operationId"] for op in operations if "observer" in op["x-allowed-roles"]} == READ_OPERATIONS


@pytest.mark.parametrize("age,status,code", [(0, 200, None), (25, 200, None),
    (25.001, 503, "STALE_DATA"), (-0.001, 503, "STALE_DATA"),
    (None, 503, "STALE_DATA"), (True, 503, "STALE_DATA")])
def test_provider_freshness_exact_boundary(client, monkeypatch, age, status, code):
    monkeypatch.setenv("SUITE_READ_TOKEN", "provider-test-observer")
    now = export["NOW"]
    source = json.loads((BASELINE / "samples.json").read_text(encoding="utf-8"))
    sample = deepcopy(next(c for c in source["cases"] if c["id"] == "runtime_healthy")["response"]["body"]["data"])
    sample["sampled_at"] = age if age is None or isinstance(age, bool) else now - age
    monkeypatch.setattr(server.runtime_client, "snapshot", lambda: sample)
    monkeypatch.setattr(server.runtime_client.time, "time", lambda: now)
    for operation in ("runtime", "metrics"):
        result = client[0].get("/api/v1/" + operation, headers=READ)
        assert result.status_code == status
        if code:
            assert result.get_json()["error"]["code"] == code and "data" not in result.get_json()


@pytest.mark.parametrize("trace_id", ["short", "with spaces here", "a" * 65, "trace-合法字符"])
def test_invalid_trace_ids_are_replaced(client, monkeypatch, trace_id):
    monkeypatch.setenv("SUITE_READ_TOKEN", "provider-test-observer")
    result = client[0].get("/api/v1/instance", headers={**READ, "X-Request-ID": trace_id})
    actual = result.headers["X-Request-ID"]
    assert actual != trace_id and re.fullmatch(r"[a-f0-9]{32}", actual)
    assert result.get_json()["meta"]["request_id"] == actual


def test_identity_survives_new_process_and_cloning_copies_identity(tmp_path):
    state = tmp_path / "state"
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: instance_identity(state), range(8)))
    assert len(set(identities)) == 1
    # A fresh interpreter models Web/worker restart without resetting state.
    program = "import sys; from pathlib import Path; from api_v1 import instance_identity; print(instance_identity(Path(sys.argv[1])))"
    result = subprocess.run([sys.executable, "-c", program, str(state)],
                            env={**os.environ, "PYTHONPATH": str(ROOT / "integrated/backend")},
                            capture_output=True, text=True, timeout=15, check=True)
    assert result.stdout.strip() == identities[0]
    clone = tmp_path / "clone"
    shutil.copytree(state, clone)
    assert instance_identity(clone) == identities[0]
    assert instance_identity(tmp_path / "new-volume") != identities[0]
