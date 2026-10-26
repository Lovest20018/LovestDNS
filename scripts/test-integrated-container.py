"""Isolated Docker smoke test; creates only labelled temporary containers/volumes."""
import argparse
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="paopaodns-suite:test")
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:10]
    name = f"suite-test-{suffix}"
    data, state = f"{name}-data", f"{name}-state"
    token = secrets.token_hex(24)
    read_token = secrets.token_hex(24)
    for volume in (data, state):
        docker("volume", "create", "--label", "paopaodns-suite-test=true", volume)
    try:
        # Seed supported legacy user configuration. No host paths or DNS port 53
        # are used, and no production container or volume is inspected. In
        # CNAUTO=yes, route the local-zone test name to recursive Unbound;
        # the forward layer deliberately does not include unbound_custom.conf.
        docker("run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", "-v", f"{data}:/data",
               args.image, "-ec", '''printf 'server:\n    local-zone: "suite-test.example." static\n    local-data: "suite-test.example. 60 IN A 192.0.2.10"\n' > /data/unbound_custom.conf
printf 'CUSTOM_VALUE="preserved"\n' > /data/custom_env.ini
printf '# legacy rule\ndomain:example.org\ndomain:suite-test.example\n' > /data/force_recurse_list.txt
cp /usr/sbin/unbound.conf /data/unbound.conf
''')

        def start(cnauto):
            docker("run", "-d", "--name", name, "--label", "paopaodns-suite-test=true",
                   "--network", "bridge", "-p", "127.0.0.1::8080", "-v", f"{data}:/data",
                   "-v", f"{state}:/var/lib/paopaodns-suite", "--memory", "768m",
                   "-e", f"WEB_UI_TOKEN={token}", "-e", f"SUITE_READ_TOKEN={read_token}", "-e", f"CNAUTO={cnauto}", "-e", "UPDATE=no",
                   "-e", "CN_TRACKER=no", "-e", "USE_MARK_DATA=no", args.image)
            binding = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
            port = binding["8080/tcp"][0]["HostPort"]
            return f"http://127.0.0.1:{port}"

        def request(base, path, body=None, auth=True, observer=False, method=None):
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = "Bearer " + (read_token if observer else token)
            data_bytes = None if body is None else json.dumps(body).encode()
            return json.load(urllib.request.urlopen(urllib.request.Request(base + path, data=data_bytes, headers=headers, method=method), timeout=15))

        def denied(base, path, expected, code=None, **kwargs):
            try:
                request(base, path, **kwargs)
                raise AssertionError(f"Expected {expected} for {path}")
            except urllib.error.HTTPError as exc:
                assert exc.code == expected, (path, exc.code)
                if code:
                    payload = json.load(exc)
                    assert payload["error"]["code"] == code and "data" not in payload, payload
                    assert payload["meta"]["request_id"] == exc.headers["X-Request-ID"]

        def wait_task(base, task_id, expected="succeeded"):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                value = request(base, f"/api/v1/maintenance/tasks/{task_id}")["data"]["task"]
                if value["state"] not in {"queued", "running"}:
                    assert value["state"] == expected, value
                    return value
                time.sleep(1)
            raise RuntimeError("Maintenance task timed out")

        def wait_ready(base):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    value = request(base, "/api/runtime")
                    if value.get("core_healthy"):
                        return value
                except Exception:
                    pass
                time.sleep(3)
            print(docker("logs", name, check=False))
            print(docker("exec", name, "/opt/venv/bin/supervisorctl", "-c", "/opt/suite/supervisord.conf", "status", check=False))
            print(docker("exec", name, "/bin/sh", "-c", "tail -n 40 /var/lib/paopaodns-suite/logs/*.log", check=False))
            raise RuntimeError("Integrated core did not become ready")

        base = start("no")
        wait_ready(base)
        print("PASS: CNAUTO=no core readiness", flush=True)
        identity = request(base, "/api/v1/instance", observer=True)["data"]["id"]
        assert request(base, "/api/v1/capabilities", observer=True)["data"]["permissions"]["configure"] is False
        denied(base, "/api/v1/status", 403, observer=True)
        denied(base, "/api/file/custom_env.ini", 401, observer=True)
        denied(base, "/api/v1/runtime", 401, auth=False)
        assert request(base, "/api/v1/openapi.json", observer=True)["openapi"] == "3.1.0"
        runtime = request(base, "/api/v1/runtime", observer=True)["data"]
        assert runtime["freshness"]["age_seconds"] <= 25
        original = request(base, "/api/v1/file/custom_env.ini")["data"]["content"]
        changed = original + 'SUITE_TEST_VALUE="new"\n'
        request(base, "/api/v1/file/custom_env.ini", {"content": changed}, method="PUT")
        revisions = request(base, "/api/v1/history/custom_env.ini")["data"]["revisions"]
        original_revision = next(item for item in revisions if item["reason"] == "before_save")["id"]
        restored = request(base, f"/api/v1/history/custom_env.ini/{original_revision}/restore", method="POST")["data"]
        assert restored["verified"] is False
        assert request(base, "/api/v1/file/custom_env.ini")["data"]["content"] == original
        compared = request(base, "/api/v1/dns-compare", {"domain": "suite-test.example", "record_type": "A", "targets": [
            {"label": "UDP", "server": "127.0.0.1", "port": 53}, {"label": "TCP", "server": "127.0.0.1", "port": 53, "transport": "tcp"}]}, observer=True)["data"]
        assert all(item["result"]["results"] == ["192.0.2.10"] for item in compared["tests"])
        assert request(base, "/api/capabilities")["edition"] == "integrated"
        answer = request(base, "/api/dns-test", {"domain": "suite-test.example", "record_type": "A"})
        assert answer["results"] == ["192.0.2.10"], answer
        assert "CUSTOM_VALUE" in request(base, "/api/file/custom_env.ini")["content"]
        print("PASS: v1 roles, freshness, UDP/TCP, config history and restore", flush=True)
        try:
            request(base, "/api/capabilities", auth=False)
            raise AssertionError("Missing authentication was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        docker("exec", name, "redis-cli", "-s", "/tmp/redis.sock", "SET", "suite-migration-marker", "preserved")
        # Redis receives SIGTERM and has time to save its RDB before recreation.
        docker("stop", "--time", "150", name)
        docker("rm", name)
        base = start("yes")
        sample = wait_ready(base)
        assert request(base, "/api/v1/instance", observer=True)["data"]["id"] == identity
        assert sample["layers"]["recursive"]["available"], sample
        assert sample["layers"]["forward"]["available"], sample
        assert docker("exec", name, "redis-cli", "-s", "/tmp/redis.sock", "GET", "suite-migration-marker") == "preserved"
        print("PASS: CNAUTO=yes, both Unbound layers, instance identity and Redis RDB retention", flush=True)
        answer = request(base, "/api/dns-test", {"domain": "suite-test.example", "record_type": "A"})
        assert answer["results"] == ["192.0.2.10"], answer
        # A bounded, real custom_mod application: create a single hosts rule,
        # submit the preview twice with the same id, then verify the target record.
        env = request(base, "/api/v1/file/custom_env.ini")["data"]["content"]
        request(base, "/api/v1/file/custom_env.ini", {"content": env + 'SUITE_LOCAL_IP="192.0.2.25"\n'}, method="PUT")
        request(base, "/api/v1/file/custom_mod.yaml", {"content": "Hosts:\n- env_key: SUITE_LOCAL_IP\n  zone: suite-host.example\n"}, method="PUT")
        plan = request(base, "/api/v1/config/apply-plan")["data"]
        payload = {"action": "apply_custom_mod", "request_id": uuid.uuid4().hex, "expected_sha256": plan["sha256"]}
        task = request(base, "/api/v1/maintenance/tasks", payload)["data"]["task"]
        assert request(base, "/api/v1/maintenance/tasks", payload)["data"]["task"]["id"] == task["id"]
        finished = wait_task(base, task["id"])
        assert finished["result"]["verified"] is False
        assert request(base, "/api/v1/dns-test", {"domain": "suite-host.example", "record_type": "A"}, observer=True)["data"]["results"] == ["192.0.2.25"]
        print("PASS: idempotent real config application and target DNS record", flush=True)
        # Reject only this candidate at the real process boundary. Restoring the
        # preceding runtime lets the actual MosDNS binary start again. This
        # touches only the disposable container, never the image or preview.
        old_runtime = docker("exec", name, "sha256sum", "/tmp/mosdns.yaml").split()[0]
        old_mod = request(base, "/api/v1/file/custom_mod.yaml")["data"]["content"]
        docker("exec", name, "/bin/sh", "-ec", '''mv /usr/sbin/mosdns /usr/sbin/mosdns.suite-test-original
cat > /usr/sbin/mosdns <<'WRAPPER'
#!/bin/sh
if [ "$1" = start ] && grep -q suite-rejected-candidate.example /tmp/mosdns.yaml; then
    exit 1
fi
exec /usr/sbin/mosdns.suite-test-original "$@"
WRAPPER
chmod 755 /usr/sbin/mosdns
''')
        try:
            rejected = "Hosts:\n- env_key: SUITE_LOCAL_IP\n  zone: suite-rejected-candidate.example\n"
            request(base, "/api/v1/file/custom_mod.yaml", {"content": rejected}, method="PUT")
            failed_plan = request(base, "/api/v1/config/apply-plan")["data"]
            failed_task = request(base, "/api/v1/maintenance/tasks", {"action": "apply_custom_mod",
                "request_id": uuid.uuid4().hex, "expected_sha256": failed_plan["sha256"]})["data"]["task"]
            failed = wait_task(base, failed_task["id"], expected="failed")
            rollback = failed["result"]["rollback"]
            assert rollback["attempted"] and rollback["runtime_restored"] and rollback["service_running"], failed
            assert docker("exec", name, "sha256sum", "/tmp/mosdns.yaml").split()[0] == old_runtime
            assert request(base, "/api/v1/file/custom_mod.yaml")["data"]["content"] == rejected
            assert request(base, "/api/v1/dns-test", {"domain": "suite-host.example", "record_type": "A"}, observer=True)["data"]["results"] == ["192.0.2.25"]
        finally:
            docker("exec", name, "/bin/sh", "-ec", "mv /usr/sbin/mosdns.suite-test-original /usr/sbin/mosdns")
            request(base, "/api/v1/file/custom_mod.yaml", {"content": old_mod}, method="PUT")
        print("PASS: failed real process start, runtime rollback, retained desired file and DNS recovery", flush=True)
        snapshot_task = request(base, "/api/v1/maintenance/tasks", {"action": "save_redis_snapshot",
            "request_id": uuid.uuid4().hex, "expected_sha256": None})["data"]["task"]
        snapshot_result = wait_task(base, snapshot_task["id"])["result"]
        assert snapshot_result["state"] == "saved" and snapshot_result["bytes"] >= 9
        print("PASS: real Redis background snapshot task", flush=True)
        # Stop and recover only a supervised test core. No host service is touched.
        ctl = ("/opt/venv/bin/supervisorctl", "-c", "/opt/suite/supervisord.conf")
        docker("exec", name, *ctl, "stop", "observer")
        try:
            for operation in ("runtime", "metrics"):
                denied(base, "/api/v1/" + operation, 503, "UNAVAILABLE", observer=True)
            assert request(base, "/api/v1/capabilities", observer=True)["data"]["features"]["runtime_snapshot"] is False
        finally:
            docker("exec", name, *ctl, "start", "observer")
        wait_ready(base)
        print("PASS: collector disconnect gives 503/UNAVAILABLE and recovers", flush=True)
        docker("exec", name, *ctl, "stop", "mosdns")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if not request(base, "/api/v1/runtime", observer=True)["data"]["core_healthy"]:
                break
            time.sleep(1)
        else:
            raise AssertionError("Stopped DNS core was reported healthy")
        docker("exec", name, *ctl, "start", "mosdns")
        wait_ready(base)
        docker("exec", name, "/opt/venv/bin/supervisorctl", "-c", "/opt/suite/supervisord.conf", "restart", "web")
        assert request(base, "/api/dns-test", {"domain": "suite-test.example", "record_type": "A"})["available"]
        print("Container smoke test passed: v1 roles, freshness, DNS comparison, history/restore, idempotent apply, failed-start rollback, collector recovery, snapshot task, config and RDB retention")
    finally:
        docker("stop", "--time", "150", name, check=False)
        docker("rm", "-f", name, check=False)
        for volume in (data, state):
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
