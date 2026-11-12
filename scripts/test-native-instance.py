"""Exercise only a marked, disposable native systemd instance on a test host."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=300).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.instance or '/' in args.instance:
        parser.error('Run as root against a named test instance')
    home = Path('/opt/lovestdns-' + args.instance) / 'current'
    spec = importlib.util.spec_from_file_location('native_installer', home / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    p = installer.paths(args.instance)
    data = p['storage'] / 'data'
    if not (data / '.lovestdns-test-instance').is_file():
        parser.error('Refusing an instance without the test-data marker')
    config = installer.read_env(p['config'])
    original_config = dict(config)
    resolver = hashlib.sha256(Path('/etc/resolv.conf').read_bytes()).hexdigest()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(config['WEB_BIND_ADDRESS'], config['WEB_BIND_ADDRESS'])
    if ':' in host:
        host = '[' + host + ']'
    base = f"http://{host}:{config['WEB_HOST_PORT']}"
    original_env = original_mod = None
    auth_cli = [str(home / 'venv/bin/python'), str(home / 'app/backend/browser_auth.py'),
                '--state-dir', str(p['storage'] / 'state'), '--external-api']
    originally_enabled = run([*auth_cli, 'status']).endswith('enabled')

    def request(path, body=None, *, observer=False, auth=True, method=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + config['SUITE_READ_TOKEN' if observer else 'WEB_UI_TOKEN']
        payload = None if body is None else json.dumps(body).encode()
        with opener.open(urllib.request.Request(base + path, data=payload, headers=headers, method=method), timeout=20) as response:
            return json.load(response)

    def denied(path, status, **kwargs):
        try:
            request(path, **kwargs)
        except urllib.error.HTTPError as exc:
            assert exc.code == status, (path, exc.code)
        else:
            raise AssertionError(f'Expected HTTP {status}: {path}')

    def restart(mode):
        installer.stop(p)
        config['CNAUTO'] = mode
        installer.write_env(p['config'], config)
        run(['systemctl', 'reset-failed', p['name'] + '.service'])
        run(['systemctl', 'start', p['name'] + '.service'])
        installer.healthy(p)

    def redis(*arguments):
        pid = run(['systemctl', 'show', p['name'] + '.service', '--property=MainPID', '--value'])
        return run(['nsenter', '-t', pid, '-m', '--', str(home / 'core/redis-cli'), '-s', '/tmp/redis.sock', *arguments])

    def ctl(*arguments):
        return run([str(home / 'venv/bin/supervisorctl'), '-c', str(p['run'] / 'supervisord.conf'), *arguments])

    def task(payload, expected='succeeded'):
        first = request('/api/v1/maintenance/tasks', payload)['data']['task']
        assert request('/api/v1/maintenance/tasks', payload)['data']['task']['id'] == first['id']
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            item = request('/api/v1/maintenance/tasks/' + first['id'])['data']['task']
            if item['state'] not in {'queued', 'running'}:
                assert item['state'] == expected, item
                return item['result']
            time.sleep(1)
        raise RuntimeError('Native maintenance task timed out')

    def query(domain, transport='udp'):
        value = request('/api/v1/dns-test', {'domain': domain, 'record_type': 'A', 'transport': transport}, observer=True)['data']
        return value['results']

    def apply(expected='succeeded'):
        plan = request('/api/v1/config/apply-plan')['data']
        return task({'action': 'apply_custom_mod', 'request_id': uuid.uuid4().hex,
                     'expected_sha256': plan['sha256']}, expected)

    try:
        run([*auth_cli, 'on'])
        restart('no')
        caps = request('/api/v1/capabilities', observer=True)['data']
        assert caps['deployment'] == 'native'
        assert caps['dns_defaults']['port'] == int(config['SUITE_DNS_PORT'])
        assert not caps['permissions']['configure']
        identity = request('/api/v1/instance', observer=True)['data']['id']
        denied('/api/v1/status', 403, observer=True)
        denied('/api/file/custom_env.ini', 401, observer=True)
        denied('/api/v1/runtime', 401, auth=False)
        assert request('/api/v1/openapi.json', observer=True)['openapi'] == '3.1.0'
        runtime = request('/api/v1/runtime', observer=True)['data']
        assert runtime['core_healthy'] and runtime['freshness']['age_seconds'] <= 25
        original_env = request('/api/v1/file/custom_env.ini')['data']['content']
        original_mod = request('/api/v1/file/custom_mod.yaml')['data']['content']
        request('/api/v1/file/custom_env.ini', {'content': original_env + 'NATIVE_HISTORY_TEST="new"\n'}, method='PUT')
        revisions = request('/api/v1/history/custom_env.ini')['data']['revisions']
        revision = next(x['id'] for x in revisions if x['reason'] == 'before_save')
        restored = request(f'/api/v1/history/custom_env.ini/{revision}/restore', method='POST')['data']
        assert restored['verified'] is False
        assert request('/api/v1/file/custom_env.ini')['data']['content'] == original_env
        for transport in ('udp', 'tcp'):
            assert query('native-test.example', transport) == ['192.0.2.10']
        print('PASS: native identity, authentication, observer permissions, history/restore and UDP/TCP recursion', flush=True)

        redis('SET', 'native-retention-marker', 'preserved')
        redis('-n', '1', 'SET', 'native-retention-marker', 'preserved-db1')
        restart('yes')
        runtime = request('/api/v1/runtime', observer=True)['data']
        assert runtime['layers']['recursive']['available'] and runtime['layers']['forward']['available']
        assert request('/api/v1/instance', observer=True)['data']['id'] == identity
        assert redis('GET', 'native-retention-marker') == 'preserved'
        assert redis('-n', '1', 'GET', 'native-retention-marker') == 'preserved-db1'
        for transport in ('udp', 'tcp'):
            assert query('native-test.example', transport) == ['192.0.2.10']
        print('PASS: split DNS, both Unbound layers, identity and both Redis databases retained after restart', flush=True)

        request('/api/v1/file/custom_env.ini', {'content': original_env + 'NATIVE_LOCAL_IP="192.0.2.25"\n'}, method='PUT')
        rule = 'Hosts:\n- env_key: NATIVE_LOCAL_IP\n  zone: native-host.example\n'
        request('/api/v1/file/custom_mod.yaml', {'content': rule}, method='PUT')
        result = apply()
        assert result['verified'] is False
        assert query('native-host.example') == ['192.0.2.25']
        print('PASS: idempotent real custom_mod application and target DNS answer', flush=True)

        binary = home / 'core/mosdns'
        original = binary.with_name('mosdns.native-test-original')
        binary.rename(original)
        try:
            binary.write_text('#!/bin/sh\nif [ "$1" = start ] && grep -q native-rejected.example /tmp/mosdns.yaml; then exit 1; fi\nexec ' + shlex.quote(str(original)) + ' "$@"\n')
            binary.chmod(0o755)
            request('/api/v1/file/custom_mod.yaml', {'content': rule.replace('native-host.example', 'native-rejected.example')}, method='PUT')
            rollback = apply('failed')['rollback']
            assert rollback['attempted'] and rollback['runtime_restored'] and rollback['service_running']
            assert query('native-host.example') == ['192.0.2.25']
        finally:
            original.replace(binary)
            request('/api/v1/file/custom_mod.yaml', {'content': rule}, method='PUT')
        print('PASS: real failed MosDNS start rolls back runtime and restores DNS', flush=True)

        saved = task({'action': 'save_redis_snapshot', 'request_id': uuid.uuid4().hex, 'expected_sha256': None})
        assert saved['state'] == 'saved' and saved['bytes'] >= 9
        print('PASS: real Redis background snapshot', flush=True)
        ctl('stop', 'observer')
        try:
            for operation in ('runtime', 'metrics'):
                denied('/api/v1/' + operation, 503, observer=True)
        finally:
            ctl('start', 'observer')
        installer.healthy(p)
        print('PASS: collector outage reports unavailable and recovers', flush=True)
    finally:
        try:
            if original_env is not None:
                request('/api/v1/file/custom_env.ini', {'content': original_env}, method='PUT')
            if original_mod is not None:
                request('/api/v1/file/custom_mod.yaml', {'content': original_mod}, method='PUT')
            # Leave the preview running with original tokens, ports and limits.
            installer.write_env(p['config'], {**original_config, 'CNAUTO': 'yes'})
            run(['systemctl', 'reset-failed', p['name'] + '.service'])
            run(['systemctl', 'restart', p['name'] + '.service'])
            installer.healthy(p)
            assert resolver == hashlib.sha256(Path('/etc/resolv.conf').read_bytes()).hexdigest()
        finally:
            run([*auth_cli, 'on' if originally_enabled else 'off'])
    print('Native instance smoke test passed; test preview remains running.', flush=True)


if __name__ == '__main__':
    main()
