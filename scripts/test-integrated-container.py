"""Isolated Docker smoke test; creates only labelled temporary containers/volumes."""
import argparse
from email.message import Message
import io
import json
from pathlib import Path
import secrets
import signal
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


def encrypted_evidence(before,after):
    deltas = {key:after[key]-before[key] for key in ('requests','rejected','dns_errors')}
    for key in ('routes','protocols','alpn'):
        deltas[key] = {name:after.get(key,{}).get(name,0)-before.get(key,{}).get(name,0)
            for name in set(before.get(key,{})) | set(after.get(key,{}))
            if after.get(key,{}).get(name,0)!=before.get(key,{}).get(name,0)}
    expected = {'requests':4,'rejected':0,'dns_errors':0,'routes':{'direct':2,'proxy':2},
                'protocols':{'HTTP/2.0':4},'alpn':{'h2':4}}
    return {'before':before,'after':after,'deltas':deltas,'valid':deltas==expected}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="lovestdns:test")
    parser.add_argument("--upgrade-image", help="Recreate on this image after the initial control run")
    parser.add_argument("--offline", action="store_true", help="Use network=none and call HTTP through docker exec")
    parser.add_argument("--cpuset-cpus", help="Optional test container CPU placement")
    parser.add_argument("--cpus", type=float, help="Optional test container CPU quota")
    parser.add_argument("--fixture-container", help="Disposable network=none DoH/SOCKS fixture container")
    parser.add_argument("--fixture-dir", type=Path, help="Read-only cert.pem and dnscrypt.toml for that fixture")
    parser.add_argument("--evidence-dir",type=Path,help="Fresh private directory for synthetic trial logs, state and data")
    args = parser.parse_args()
    def interrupted(*_): raise SystemExit('Container trial interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    if bool(args.fixture_container) != bool(args.fixture_dir) or args.fixture_container and not args.offline:
        parser.error('Fixture container and directory must be supplied together with --offline')
    fixture_mount = []
    if args.fixture_dir:
        fixture_mount = ['--mount',f'type=bind,source={args.fixture_dir.resolve()},target=/trial-fixture,readonly']
        details = json.loads(docker('inspect',args.fixture_container))[0]
        if details['HostConfig']['NetworkMode'] != 'none' or not details['State']['Running']:
            parser.error('Fixture must be running with network=none')
    suffix = uuid.uuid4().hex[:10]
    name = f"suite-test-{suffix}"
    data, state = f"{name}-data", f"{name}-state"
    if args.evidence_dir:
        args.evidence_dir.mkdir(parents=True,exist_ok=False)
        (args.evidence_dir/'trial.json').write_text(json.dumps({'name':name,'image':args.image,'upgrade_image':args.upgrade_image,
            'offline':args.offline,'fixture_container':args.fixture_container,'synthetic_only':True},indent=2)+'\n')
    token = secrets.token_hex(24)
    read_token = secrets.token_hex(24)
    for volume in (data, state):
        docker("volume", "create", "--label", "paopaodns-suite-test=true", volume)
    try:
        # Seed supported legacy user configuration. No host paths or DNS port 53
        # are used, and no production container or volume is inspected. In
        # CNAUTO=yes, route the local-zone test name to recursive Unbound;
        # the forward layer deliberately does not include unbound_custom.conf.
        docker("run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", "-v", f"{data}:/data", *fixture_mount,
               args.image, "-ec", '''printf 'server:\n    local-zone: "suite-test.example." static\n    local-data: "suite-test.example. 60 IN A 192.0.2.10"\n' > /data/unbound_custom.conf
printf 'CUSTOM_VALUE="preserved"\n' > /data/custom_env.ini
printf '# legacy rule\ndomain:example.org\ndomain:suite-test.example\n' > /data/force_recurse_list.txt
cp /usr/sbin/unbound.conf /data/unbound.conf
if [ -f /trial-fixture/dnscrypt.toml ]; then cp /trial-fixture/dnscrypt.toml /data/dnscrypt.toml; fi
''')

        def start(cnauto, image=None):
            network = ("--network", "none") if args.offline else ("--network", "bridge", "-p", "127.0.0.1::8080")
            if args.fixture_container: network = ('--network','container:'+args.fixture_container)
            resources = (["--cpuset-cpus", args.cpuset_cpus] if args.cpuset_cpus else [])
            if args.cpus is not None: resources += ["--cpus", str(args.cpus)]
            fixture_env = ['-e','SSL_CERT_FILE=/trial-fixture/cert.pem','-e','SOCKS5=127.0.0.1:1080'] if args.fixture_dir else []
            docker("run", "-d", "--name", name, "--label", "paopaodns-suite-test=true",
                   *network, *fixture_mount, *fixture_env, "-v", f"{data}:/data",
                   "-v", f"{state}:/var/lib/paopaodns-suite", "--memory", "768m",
                   *resources,
                   "-e", f"WEB_UI_TOKEN={token}", "-e", f"SUITE_READ_TOKEN={read_token}", "-e", f"CNAUTO={cnauto}", "-e", "UPDATE=no",
                   "-e", "CN_TRACKER=no", "-e", "USE_MARK_DATA=no", image or args.image)
            # Disposable test volume explicitly opts into token API coverage.
            docker("exec", name, "/opt/venv/bin/python", "-c",
                   "from pathlib import Path; from backend.browser_auth import set_external_api; set_external_api(Path('/var/lib/paopaodns-suite'), True)")
            if args.offline:
                return "http://127.0.0.1:8080"
            binding = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))
            port = binding["8080/tcp"][0]["HostPort"]
            return f"http://127.0.0.1:{port}"

        def request(base, path, body=None, auth=True, observer=False, method=None):
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = "Bearer " + (read_token if observer else token)
            if args.offline:
                code = '''import json,sys,urllib.request,urllib.error
x=json.load(sys.stdin)
data=None if x['body'] is None else json.dumps(x['body']).encode()
request=urllib.request.Request(x['url'],data=data,headers=x['headers'],method=x['method'])
try: response=urllib.request.urlopen(request,timeout=15)
except urllib.error.HTTPError as error: response=error
print(json.dumps({'status':response.code,'headers':dict(response.headers),'body':response.read().decode()}))
'''
                called = subprocess.run(["docker", "exec", "-i", name, "/opt/venv/bin/python", "-c", code],
                    input=json.dumps({'url':base+path,'body':body,'headers':headers,'method':method}),
                    text=True,capture_output=True,timeout=25,check=True)
                response = json.loads(called.stdout)
                if response['status'] >= 400:
                    response_headers = Message()
                    for key,value in response['headers'].items(): response_headers[key] = value
                    raise urllib.error.HTTPError(base+path,response['status'],'container HTTP error',
                        response_headers,io.BytesIO(response['body'].encode()))
                return json.loads(response['body'])
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

        def encrypted_paths(base, phase):
            if not args.fixture_container: return
            def counters():
                code = "import json,urllib.request; print(urllib.request.urlopen(json.load(open('/trial-fixture/fixture.json'))['state_url']).read().decode())"
                return json.loads(docker('exec',name,'/opt/venv/bin/python','-c',code))
            before = counters()
            for port in (5302,5303):
                for kind,expected in (('A','192.0.2.10'),('AAAA','2001:db8::10')):
                    # Cold UDP must finish before TCP checks cache reuse; the
                    # comparison API intentionally runs multiple targets in parallel.
                    for transport in ('udp','tcp'):
                        answer = request(base,'/api/v1/dns-compare',{'domain':f'{phase}-{port}-{kind.lower()}.cache.test','record_type':kind,
                            'targets':[{'label':transport,'server':'127.0.0.1','port':port,'transport':transport}]},observer=True)['data']
                        assert len(answer['tests']) == 1 and answer['tests'][0]['result']['results']==[expected]
            after = counters()
            observed = encrypted_evidence(before,after)
            if args.evidence_dir:
                (args.evidence_dir/('protocol-'+phase+'.json')).write_text(json.dumps(observed,indent=2)+'\n')
            assert observed['valid'],observed['deltas']
            code = '''import hashlib,json,pathlib,tomllib
binary=pathlib.Path('/usr/bin/dnscrypt-proxy')
pids=[p.name for p in pathlib.Path('/proc').iterdir() if p.name.isdigit() and (p/'exe').exists() and str((p/'exe').resolve())==str(binary)]
assert len(pids)==2,pids
info=json.loads(pathlib.Path('/opt/suite/build-info.json').read_text())
if info['trial']['case']=='combined':
 assert hashlib.sha256(binary.read_bytes()).hexdigest()==info['components']['source_manifest']['binary_sha256']
 for name in ('dnscrypt.toml','dnscrypt_socks.toml'):
  conf=tomllib.loads((pathlib.Path('/data/dnscrypt-resolvers')/name).read_text())
  assert conf['pqdnscrypt'] is False and conf['lb_strategy']=='p2'
print(json.dumps({'case':info['trial']['case'],'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'running_pids':pids}))
'''
            print('PASS: encrypted paths '+phase+' '+docker('exec',name,'/opt/venv/bin/python','-c',code),flush=True)

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
        base = start("yes", args.upgrade_image)
        sample = wait_ready(base)
        assert request(base, "/api/v1/instance", observer=True)["data"]["id"] == identity
        assert sample["layers"]["recursive"]["available"], sample
        assert sample["layers"]["forward"]["available"], sample
        assert docker("exec", name, "redis-cli", "-s", "/tmp/redis.sock", "GET", "suite-migration-marker") == "preserved"
        print("PASS: CNAUTO=yes, both Unbound layers, instance identity and Redis RDB retention", flush=True)
        assert 'CUSTOM_VALUE="preserved"' in request(base,"/api/file/custom_env.ini")["content"]
        if args.upgrade_image:
            print("PASS: recreated from control to upgrade image with the same data/state volumes", flush=True)
        encrypted_paths(base,'upgraded')
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
        if args.upgrade_image:
            docker("stop", "--time", "150", name)
            docker("rm", name)
            base = start("yes")
            wait_ready(base)
            assert request(base, "/api/v1/instance", observer=True)["data"]["id"] == identity
            assert docker("exec", name, "redis-cli", "-s", "/tmp/redis.sock", "GET", "suite-migration-marker") == "preserved"
            assert 'CUSTOM_VALUE="preserved"' in request(base,"/api/file/custom_env.ini")["content"]
            assert request(base, "/api/dns-test", {"domain": "suite-test.example", "record_type": "A"})["results"] == ["192.0.2.10"]
            encrypted_paths(base,'returned')
            print("PASS: returned to control image with retained identity, RDB marker and working DNS", flush=True)
        print("Container smoke test passed: v1 roles, freshness, DNS comparison, history/restore, idempotent apply, failed-start rollback, collector recovery, snapshot task, config and RDB retention")
    finally:
        if args.evidence_dir:
            try:
                inspected = docker('inspect',name,check=False)
                if inspected:
                    info = json.loads(inspected)[0]
                    (args.evidence_dir/'container.json').write_text(json.dumps({k:info[k] for k in ('Id','Image','State','HostConfig')},indent=2)+'\n')
                    (args.evidence_dir/'container.log').write_text(docker('logs',name,check=False)+'\n')
                    for source,destination in (('/var/lib/paopaodns-suite','state'),('/data','data')):
                        copied = subprocess.run(['docker','cp',name+':'+source,str(args.evidence_dir/destination)],text=True,capture_output=True,timeout=60)
                        (args.evidence_dir/(destination+'-copy.log')).write_text(copied.stdout+copied.stderr)
            except Exception as exc:
                (args.evidence_dir/'capture-error.txt').write_text(repr(exc)+'\n')
        docker("stop", "--time", "150", name, check=False)
        docker("rm", "-f", name, check=False)
        for volume in (data, state):
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
