"""Export/check the v1 provider baseline without network or a running service."""
import argparse
import builtins
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/contracts/v1"
NOW = 1792972800.0  # Fixed Unix UTC seconds: 2026-10-26T00:00:00Z.
IDENTITY = "00000000-0000-4000-8000-000000000001"
EXCLUDED = {"node_modules", "dist", "dist-ssr", "__pycache__", ".pytest_cache", ".vite"}
SOURCE_DIRS = ("integrated", "packaging/integrated", "packaging/native", "packaging/components", "scripts", "web")
SOURCE_ROOT_FILES = (".gitignore", ".gitattributes", ".dockerignore", "install.py", ".env.integrated.example", ".env.native.example", "docker-compose.integrated.yaml",
                     ".github/workflows/integrated-check.yml", "packaging/upstream.lock.json",
                     "packaging/native/requirements.lock.txt",
                     "docs/lovestdns-native-deployment.md",
                     "packaging/standalone.freeze.json", "docker-compose-web.yaml",
                     "docker-compose.sidecar.yaml", "packaging/legacy/web-docker-ghcr.yml.disabled",
                     ".github/workflows/native-package.yml", "packaging/components.lock.json", "packaging/dnscrypt-source.lock.json")
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".json", ".html", ".md", ".yaml", ".yml", ".svg", ".txt", ".patch"}
BINARY_SUFFIXES = {".png"}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_summary():
    paths = [ROOT / name for name in SOURCE_ROOT_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not EXCLUDED.intersection(p.relative_to(ROOT).parts)
                     and (p.suffix in TEXT_SUFFIXES | BINARY_SUFFIXES or p.name in {"Dockerfile", ".gitignore"}))
    files = {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes() if p.suffix in BINARY_SUFFIXES else p.read_bytes().replace(b"\r\n", b"\n"))
             for p in sorted(set(paths))}
    return {"sha256": sha256(encode(files)), "files": files,
            "hash_mode": "SHA-256 of UTF-8 sorted/indented files map with final LF; text files use LF-normalized bytes and binary files use original bytes",
            "scope": [*SOURCE_DIRS, *SOURCE_ROOT_FILES],
            "commit_lookup": "git log -1 --format=%H -- docs/contracts/v1/manifest.json"}


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz=tz or timezone.utc)


def dns_packet(query, server, port, tcp=False):
    """Synthetic wire input; keep real query validation, DNS parser and handlers."""
    missing = b"missing" in query or server == "192.0.2.54"
    header = struct.pack("!HHHHHH", int.from_bytes(query[:2], "big"),
                         0x8183 if missing else 0x8180, 1, 0 if missing else 1, 0, 0)
    answer = b"" if missing else b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([192, 0, 2, 53])
    return header + query[12:] + answer


def collect_samples(server, stack, temp):
    from api_v1 import LOGGER
    from runtime.observer import rate

    # Production runs on Linux. Keep synthetic configuration bytes identical
    # when replaying the real write handler on Windows; revision/ETag hash raw
    # bytes, so the host's automatic text newline translation is observable.
    real_open = builtins.open

    def fixture_open(file, mode='r', *args, **kwargs):
        if mode == 'w' and isinstance(file, (str, os.PathLike)) and Path(file).parent == temp / 'data':
            kwargs['newline'] = '\n'
        return real_open(file, mode, *args, **kwargs)

    stack.enter_context(patch('builtins.open', fixture_open))

    # Patch only environment/I/O boundaries. Every saved response runs through
    # Flask routing, authentication, envelope creation and the actual handler.
    for target, name, value in (
        (server, "STATE_DIR", temp / "state"), (server, "RUN_DIR", temp / "run"),
        (server, "history", server.History(temp / "state")),
        (server.base, "DATA_DIR", str(temp / "data")),
        (server.base, "WEB_UI_TOKEN", "contract-test-admin"),
        (server.base, "WEB_UI_ALLOW_NO_AUTH", False), (server.base, "datetime", FixedDatetime),
        (server.base, "_exchange_dns", dns_packet), (LOGGER, "disabled", True),
    ):
        stack.enter_context(patch.object(target, name, value))
    stack.enter_context(patch("time.time", return_value=NOW))
    stack.enter_context(patch("time.monotonic", return_value=100.0))
    stack.enter_context(patch("api_v1.uuid.uuid4", return_value=uuid.UUID(IDENTITY)))
    stack.enter_context(patch.dict(server.app.config, TESTING=True))
    stack.enter_context(patch.object(server, "boot_env", return_value={"CNAUTO": "yes"}))
    stack.enter_context(patch('api_v1.external_api_enabled', return_value=True))
    lock = json.loads((ROOT / "packaging/upstream.lock.json").read_text(encoding="utf-8"))
    stack.enter_context(patch.object(server, "build_info", return_value={k: lock[k] for k in ("image", "source_commit")}))
    stack.enter_context(patch.object(server.app, "_static_folder", str(temp / 'dist')))
    stack.enter_context(patch.dict(os.environ, {'LOVESTDNS_SYSTEMD': '0'}))
    stack.enter_context(patch.object(server.runtime_client, "manager_request", return_value={
        "apply_custom_mod": True, "save_redis_snapshot": True}))
    counters = {"total.num.queries": 10, "total.num.cachehits": 8, "total.num.cachemiss": 2, "time.up": 100}
    current = {**counters, "time.up": 105}
    sample = {"schema_version": 1, "sampled_at": NOW - 2, "interval_seconds": 5,
              "ready": True, "core_healthy": True,
              "components": {name: {"statename": "RUNNING", "pid": i + 10, "start": NOW - 100, "exitstatus": 0}
                             for i, name in enumerate(("unbound-raw", "unbound-forward", "redis", "mosdns", "dnscrypt", "watcher"))},
              "layers": {layer: {"available": True, "counters": current,
                                  **rate(counters, current, 5, True)} for layer in ("recursive", "forward")},
              "redis": {"available": True, "info": {"used_memory": 1048576, "loading": 0}},
              "persistence": {"scope": "Redis RDB", "last_save_time": NOW - 60,
                              "last_save_status": "ok", "warnings": []}}
    snapshot = stack.enter_context(patch.object(server.runtime_client, "snapshot", return_value=sample))
    cases = []

    with server.app.test_client() as client:
        def capture(case_id, path, operation, *, method="GET", body=None, status=200,
                    role="observer", scenario="fresh synthetic collector snapshot"):
            request_id = "contract-" + case_id
            headers = {"X-Request-ID": request_id}
            if role == "observer":
                headers["Authorization"] = "Bearer contract-test-observer"
            elif role == "admin":
                headers["Authorization"] = "Bearer contract-test-admin"
            response = client.open("/api/v1" + path, method=method, headers=headers,
                                   **({"json": body} if body is not None else {}))
            if response.status_code != status:
                raise ValueError(f"{case_id}: expected {status}, got {response.status_code}: {response.get_json()}")
            request_data = {"method": method, "path": "/api/v1" + path, "role": role,
                            "headers": {"X-Request-ID": request_id}}
            if body is not None:
                request_data["body"] = body
            cases.append({"id": case_id, "operation_id": operation,
                          "provenance": {"response": "real_flask_handler", "scenario": "synthetic_test",
                                         "inputs": scenario}, "request": request_data,
                          "response": {"status": response.status_code,
                                       "headers": {key: response.headers[key] for key in ("X-Request-ID", "Cache-Control")},
                                       "body": response.get_json()}})
            if response.headers.get('ETag'):
                cases[-1]['response']['headers']['ETag'] = response.headers['ETag']

        capture("observer_instance", "/instance", "instance", scenario="new temporary state volume; fixed UUID generator")
        capture("observer_capabilities", "/capabilities", "capabilities", scenario="collector and manager available; public upstream lock metadata")
        capture("runtime_healthy", "/runtime", "runtime")
        capture("metrics_zero", "/metrics", "metrics", scenario="unchanged query counters over 5 seconds; real observer.rate returns QPS=0 and ratio=null")
        changed = deepcopy(sample)
        for layer, reason in (("recursive", "process_changed"), ("forward", "first_sample")):
            changed["layers"][layer].update(rate(None if reason == "first_sample" else counters, current, 5, False))
        snapshot.return_value = changed
        capture("metrics_not_comparable", "/metrics", "metrics", scenario="synthetic process restart and first sample; real observer.rate")
        partial = deepcopy(sample)
        partial["core_healthy"] = False
        partial["components"]["unbound-forward"]["statename"] = "STOPPED"
        partial["layers"]["forward"] = {"available": False, "error": "component unavailable in test scenario"}
        partial["redis"] = {"available": False, "error": "component unavailable in test scenario"}
        snapshot.return_value = partial
        capture("runtime_partial", "/runtime", "runtime", scenario="fresh evidence with unhealthy components; HTTP 200 is not core health")
        capture("metrics_partial", "/metrics", "metrics", scenario="recursive zero; forward and Redis unavailable -> null")
        snapshot.return_value = {**sample, "sampled_at": NOW - 26}
        for operation in ("runtime", "metrics"):
            capture(operation + "_stale", "/" + operation, operation, status=503, scenario="26-second-old synthetic snapshot; real sample_age rejects age >25")
        snapshot.return_value = {"available": False, "error": "collector unavailable in test scenario"}
        capture("capabilities_unavailable", "/capabilities", "capabilities", scenario="collector unavailable; permission-aware capability response")
        for operation in ("runtime", "metrics"):
            capture(operation + "_unavailable", "/" + operation, operation, status=503, scenario="collector I/O failure substituted at snapshot boundary")
        capture("unauthorized", "/instance", "instance", role="anonymous", status=401, scenario="no Authorization header")
        capture("forbidden_config", "/file/custom_env.ini", "file_read", status=403, scenario="observer credential; real config-read authorization guard")
        query_status = {'enabled': False, 'active': False, 'phase': 'disabled', 'reason': None,
            'sampled_at': NOW, 'session_id': 'synthetic-recorder-session',
            'source': 'linux_passive_entry_packets', 'scope': 'service_network_namespace',
            'timing_scope': 'passive_entry_packet_interval', 'endpoints': [],
            'counters': {}, 'queue_depth': 0, 'retained_rows': 0, 'database_bytes': 16384,
            'memory_limits': {'event_queue': 512, 'pending_queries': 2048, 'tcp_streams': 128, 'response_wait_seconds': 10},
            'limits': {'retention_seconds': 86400, 'rows': 100000, 'storage_bytes': 67108864, 'database_bytes': 32505856}}
        with patch.object(server.runtime_client, 'query_records_request', return_value=query_status):
            capture('query_recording_default_off', '/query-recording', 'query_recording_status', role='admin',
                    scenario='synthetic recorder I/O, default-off status; actual administrator-only handler')
            capture('query_recording_observer_forbidden', '/query-recording', 'query_recording_status', status=403,
                    scenario='real guard denies query history to external observer credentials')
        query_event = {'id': 'synthetic-recorder-session:1', 'sequence': 1, 'observed_at': NOW - 1,
            'response_at': NOW - .999, 'source_ip': '192.0.2.2', 'source_port': 32000,
            'entry_address': '192.0.2.1', 'entry_port': 15453, 'component': 'mosdns', 'transport': 'udp',
            'domain': 'example.test.', 'qtype': 1, 'qclass': 1, 'record_type': 'A', 'same_key_packets': 1,
            'rcode': 'NOERROR', 'rcode_value': 0, 'answer_count': 1, 'addresses': ['192.0.2.8'],
            'addresses_complete': True, 'truncated': False, 'elapsed_ms': 1.0,
            'outcome': 'response_observed', 'timing_scope': 'passive_entry_packet_interval', 'branch': None, 'cache_hit': None}
        with patch.object(server.runtime_client, 'query_records_request', return_value={
            'status': {**query_status, 'retained_rows': 1}, 'events': [query_event], 'next_before': None}):
            capture('query_records_recent', '/query-records', 'query_records_recent', role='admin',
                    scenario='synthetic passive event at recorder I/O boundary; no live traffic or cache/path inference')
        with patch.object(server.runtime_client, 'query_records_request', return_value={**query_status, 'enabled': True, 'phase': 'starting'}):
            capture('query_recording_opt_in', '/query-recording', 'query_recording_configure', method='PUT', role='admin',
                    body={'enabled': True}, scenario='synthetic recorder I/O; actual handler submits explicit opt-in')
        with patch.object(server.runtime_client, 'query_records_request', return_value=query_status):
            capture('query_records_clear', '/query-records', 'query_records_clear', method='DELETE', role='admin',
                    scenario='synthetic empty recorder status; actual administrator clear handler')
        with patch.object(server.runtime_client, 'query_records_request', side_effect=RuntimeError('查询记录服务暂不可用')):
            capture('query_records_unavailable', '/query-records', 'query_records_recent', role='admin', status=503,
                    scenario='synthetic recorder I/O failure; actual handler returns UNAVAILABLE without fabricated rows')
        with patch('api_v1.external_api_enabled', return_value=False):
            capture('external_api_disabled', '/instance', 'instance', role='admin', status=403,
                    scenario='external token access explicitly disabled; real authentication switch')
        (temp / 'data').mkdir(parents=True, exist_ok=True)
        capture('config_create', '/file/custom_env.ini', 'file_write', method='PUT', role='admin',
                body={'content': 'CNAUTO="yes"\n', 'expected_revision': 'missing'},
                scenario='new temporary configuration file; real history, write lock and missing-file condition')
        capture('config_read', '/file/custom_env.ini', 'file_read', role='admin',
                scenario='temporary saved file; raw-byte SHA-256 revision and strong ETag')
        capture('config_conflict', '/file/custom_env.ini', 'file_write', method='PUT', role='admin', status=412,
                body={'content': 'CNAUTO="no"\n', 'expected_revision': 'missing'},
                scenario='stale missing-file condition; real locked write rejects overwrite')
        with patch.object(server.runtime_client, 'manager_request', return_value={
            'files': [{'filename': 'custom_env.ini', 'revision': sha256(b'CNAUTO="yes"\n'),
                       'state': 'unknown', 'recorded_revision': None, 'recorded_at': None, 'action': None, 'verified': False}],
            'sampled_at': NOW, 'scope': 'synthetic manager state; no successful action receipt', 'verified': False}):
            capture('config_state_unknown', '/config/state', 'config_state', role='admin',
                    scenario='synthetic manager I/O; real administrator-only state handler')
        for case_id, domain in (("dns_success", "example.com"), ("dns_nxdomain", "missing.example")):
            capture(case_id, "/dns-test", "dns_query", method="POST",
                    body={"domain": domain, "record_type": "A", "server": "127.0.0.1", "port": 53},
                    scenario="synthetic DNS wire packet at exchange boundary; real lookup/parser; fixed clock yields 0ms")
        capture("dns_compare_mixed", "/dns-compare", "dns_compare", method="POST",
                body={"domain": "example.com", "record_type": "A", "targets": [
                    {"label": "core", "server": "127.0.0.1", "port": 53, "transport": "udp"},
                    {"label": "comparison", "server": "192.0.2.54", "port": 53, "transport": "tcp"}]},
                scenario="two synthetic wire replies: NOERROR and NXDOMAIN; real parallel comparison handler")
        capture("dns_health", "/health-check", "dns_health", scenario="synthetic successful A replies for actual built-in sample domains; real health handler")
        capture('gateway_token_forbidden', '/gateway/config', 'gateway_config', status=403,
                scenario='external observer credential cannot access browser-only optional integration')
        from browser_auth import COOKIE, create_session, database, reset_password
        reset_password(temp / 'state', 'synthetic-browser-contract-password')
        with database(temp / 'state') as db:
            cookie, _csrf = create_session(db, False)
        client.set_cookie(COOKIE, cookie)
        capture('gateway_default_off', '/gateway/config', 'gateway_config', role='browser_admin',
                scenario='ephemeral browser administrator session; no configured gateway and no network requests')
        capture('gateway_disabled_summary', '/gateway/summary', 'gateway_summary', role='browser_admin', status=409,
                scenario='optional gateway is disabled; rejects observation without external I/O')
        client.delete_cookie(COOKIE)
        document = client.get("/api/v1/openapi.json", headers={
            "Authorization": "Bearer contract-test-observer", "X-Request-ID": "contract-openapi"}).get_json()
    return document, {"format_version": 1, "api_version": "1", "candidate_version": server.VERSION,
                      "normalization": {"unix_utc_seconds": NOW, "instance_id": IDENTITY,
                                        "request_ids": "explicit contract-<case id>",
                                        "elapsed_ms": "fixed monotonic clock; zero is synthetic",
                                        "network": "no network; documentation IPs and public example domains",
                                        "configuration_bytes": "temporary UTF-8 files use LF on every host, matching Linux production writes",
                                        "credentials": "ephemeral test credentials never serialized"}, "cases": cases}


def validate_samples(document, samples):
    from jsonschema import Draft202012Validator, FormatChecker

    for schema in document["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    operations = {operation["operationId"]: (path, method.upper(), operation)
                  for path, methods in document["paths"].items() for method, operation in methods.items()}
    for case in samples["cases"]:
        path, method, operation = operations[case["operation_id"]]
        request, response = case["request"], case["response"]
        route_pattern = re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape("/api/v1" + path))
        if method != request["method"] or not re.fullmatch(route_pattern, request["path"]):
            raise ValueError(f"{case['id']}: request route differs from OpenAPI")
        def validate(schema, value):
            Draft202012Validator({**schema, "components": document["components"]},
                                 format_checker=FormatChecker()).validate(value)
        if "body" in request:
            validate(operation["requestBody"]["content"]["application/json"]["schema"], request["body"])
        spec = operation["responses"][str(response["status"])]
        validate(spec["content"]["application/json"]["schema"], response["body"])
        for name, header in spec["headers"].items():
            validate(header["schema"], response["headers"][name])
        trace_id = request["headers"]["X-Request-ID"]
        if trace_id != response["headers"]["X-Request-ID"] or trace_id != response["body"]["meta"]["request_id"]:
            raise ValueError(f"{case['id']}: request ID mismatch")
        if response["status"] >= 400 and "data" in response["body"]:
            raise ValueError(f"{case['id']}: error must not carry current data")


def build_bundle():
    sys.path[:0] = [str(ROOT / p) for p in ("integrated/backend", "integrated", "web/backend")]
    # Ignore any real credentials, data paths and DNS defaults inherited by the
    # invoking shell. Import and all handler calls remain inside this context.
    with TemporaryDirectory(prefix="paopaodns-contract-") as directory, ExitStack() as stack:
        temp = Path(directory)
        stack.enter_context(patch.dict(os.environ, {"WEB_UI_TOKEN": "contract-test-admin",
            "SUITE_READ_TOKEN": "contract-test-observer", "SUITE_INSTANCE_NAME": "Contract DNS",
            "DNS_TEST_PORT": "53", "DATA_DIR": str(temp / "data"),
            "SUITE_STATE_DIR": str(temp / "state"), "SUITE_RUN_DIR": str(temp / "run")}, clear=True))
        import server
        # server may already be imported by pytest; override imported defaults too.
        stack.enter_context(patch.object(server.base, "DNS_TEST_PORT", 53))
        document, samples = collect_samples(server, stack, temp)
        validate_samples(document, samples)
    payloads = {"openapi.json": encode(document), "samples.json": encode(samples)}
    manifest = {"format_version": 1, "api_version": "1", "candidate_version": server.VERSION,
                "status": "development_candidate", "stable_release": False,
                "container_runtime_acceptance": "isolated_linux_amd64_passed_for_recorded_revision",
                "runtime_acceptance_evidence": {
                    "tested_runtime_commit": None,
                    "report": "docs/verification.md",
                    "scope": "Historical local evidence is retained privately; public scripts permit independent reproduction and do not certify future changes"},
                "source": source_summary(),
                "artifacts": {name: {"sha256": sha256(data), "hash_mode": "UTF-8 canonical JSON with LF"}
                              for name, data in payloads.items()},
                "sample_count": len(samples["cases"]),
                "provenance": "All responses from actual Flask handlers with synthetic I/O and clocks; no live core/network acceptance evidence",
                "standalone_checkpoint": "standalone-20261024"}
    return {**payloads, "manifest.json": encode(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Regenerate reviewed artifacts")
    mode.add_argument("--check", action="store_true", help="Fail on source, schema or response drift; write nothing")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    bundle = build_bundle()
    if args.write:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, data in bundle.items():
            (args.output / name).write_bytes(data)
    else:
        changed = [name for name, data in bundle.items() if not (args.output / name).is_file()
                   or (args.output / name).read_bytes().replace(b"\r\n", b"\n") != data]
        if changed:
            raise SystemExit("Provider baseline drift: " + ", ".join(changed) + "; review changes before --write")
    manifest = json.loads(bundle["manifest.json"])
    print(f"v1 provider baseline {'exported' if args.write else 'verified'}: "
          f"{manifest['candidate_version']}, {manifest['sample_count']} samples, source {manifest['source']['sha256']}")


if __name__ == "__main__":
    main()
