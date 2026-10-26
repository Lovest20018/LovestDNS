"""Export/check the v1 provider baseline without network or a running service."""
import argparse
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
SOURCE_DIRS = ("integrated", "packaging/integrated", "scripts", "web")
SOURCE_ROOT_FILES = (".gitignore", ".gitattributes", ".dockerignore", "docker-compose.integrated.yaml",
                     ".github/workflows/integrated-check.yml", "packaging/upstream.lock.json",
                     "packaging/standalone.freeze.json", "docker-compose-web.yaml",
                     "docker-compose.sidecar.yaml", ".github/workflows/web-docker-ghcr.yml")
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".css", ".json", ".html", ".md", ".yaml", ".yml", ".svg"}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_summary():
    paths = [ROOT / name for name in SOURCE_ROOT_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not EXCLUDED.intersection(p.relative_to(ROOT).parts)
                     and (p.suffix in TEXT_SUFFIXES or p.name in {"Dockerfile", ".gitignore"}))
    files = {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes().replace(b"\r\n", b"\n"))
             for p in sorted(set(paths))}
    return {"sha256": sha256(encode(files)), "files": files,
            "hash_mode": "SHA-256 of UTF-8 sorted/indented files map with final LF; each source file uses LF-normalized bytes",
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

    # Patch only environment/I/O boundaries. Every saved response runs through
    # Flask routing, authentication, envelope creation and the actual handler.
    for target, name, value in (
        (server, "STATE_DIR", temp / "state"), (server, "RUN_DIR", temp / "run"),
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
    lock = json.loads((ROOT / "packaging/upstream.lock.json").read_text(encoding="utf-8"))
    stack.enter_context(patch.object(server, "build_info", return_value={k: lock[k] for k in ("image", "source_commit")}))
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
        document = client.get("/api/v1/openapi.json", headers={
            "Authorization": "Bearer contract-test-observer", "X-Request-ID": "contract-openapi"}).get_json()
    return document, {"format_version": 1, "api_version": "1", "candidate_version": server.VERSION,
                      "normalization": {"unix_utc_seconds": NOW, "instance_id": IDENTITY,
                                        "request_ids": "explicit contract-<case id>",
                                        "elapsed_ms": "fixed monotonic clock; zero is synthetic",
                                        "network": "no network; documentation IPs and public example domains",
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
                "container_runtime_acceptance": "not_completed", "source": source_summary(),
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
