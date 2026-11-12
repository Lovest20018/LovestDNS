"""Only synthetic loopback data; never loads a real service or credentials."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import time

root = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(root / 'scripts/ui-optimization-fixture.py'), run_name='home_fixture')
server, data = fixture['server'], fixture['data']
os.environ['SUITE_INSTANCE_NAME'] = 'LovestDNS Native Test — 用于验证长实例名称布局'
tasks = []
controls = {'config_error': False, 'identity': 'named'}
from runtime.config_state import FILES
from flask import jsonify, request


@server.app.post('/fixture/home-control')
def home_control():
    body = request.get_json()
    if 'config_error' in body:
        controls['config_error'] = bool(body['config_error'])
    if body.get('identity') in ('named', 'unknown', 'mismatch'):
        controls['identity'] = body['identity']
    return jsonify({'ok': True})


def installation():
    unknown = controls['identity'] == 'unknown'
    return {'source': 'unavailable' if unknown else 'native_service_environment',
            'service_name': None if unknown else 'lovestdns-synthetic.service',
            'control_command': None if unknown else '/usr/local/sbin/lovestdns-syntheticctl',
            'config_file': None if unknown else '/etc/lovestdns-synthetic/lovestdns-synthetic.env',
            'data_dir': None if unknown else '/var/lib/lovestdns-synthetic/data',
            'state_dir': None if unknown else '/var/lib/lovestdns-synthetic/state',
            'release_path': None if unknown else '/opt/lovestdns-synthetic/releases/synthetic.2',
            'receipt': {'status': 'missing' if unknown else 'mismatch' if controls['identity'] == 'mismatch' else 'recorded',
                        'archive_sha256': None if unknown or controls['identity'] == 'mismatch' else 'c' * 64,
                        'manifest_sha256': None if unknown else 'd' * 64, 'installed_at': None if unknown else 1794432000.0}}


server.installation_summary = installation
server.build_info = lambda: {'image': 'synthetic-upstream', 'source_commit': 'e' * 40,
    'native_refresh': {'source_commit': 'a' * 40, 'source_sha256': 'a' * 64,
                       'method': 'synthetic-app-ui', 'base_version': 'synthetic.base', 'base_archive_sha256': 'b' * 64},
    'components': {'selected': ['redis', 'unbound', 'dnscrypt'],
                   'versions': {'redis': '7.2.17', 'unbound': '1.26.2', 'dnscrypt': '2.1.20'}, 'lock_sha256': 'e' * 64}}


def versions():
    return [{'filename': name, 'revision': hashlib.sha256((data / name).read_bytes()).hexdigest() if (data / name).exists() else 'missing',
             'state': 'recorded' if tasks else 'unknown', 'recorded_revision': None,
             'recorded_at': tasks[0]['updated'] if tasks else None, 'action': tasks[0]['action'] if tasks else None, 'verified': False} for name in FILES]


def management(action, *args):
    if action == 'capabilities':
        return {'apply_custom_mod': True, 'save_redis_snapshot': True, 'restart_dns_core': True}
    if action == 'tasks':
        return {'tasks': tasks}
    if action == 'config_state':
        if controls['config_error']:
            raise RuntimeError('Synthetic configuration read failure')
        return {'files': versions(), 'scope': 'synthetic browser fixture', 'sampled_at': time.time(), 'verified': False}
    if action == 'submit':
        body = args[0]
        existing = next((task for task in tasks if task['id'] == body['request_id']), None)
        if existing:
            return {'task': existing}
        task = {'id': body['request_id'], 'action': body['action'], 'state': 'succeeded', 'created': time.time(), 'updated': time.time(),
                'result': {'message': '合成测试任务完成，未重启任何服务', 'verified': False}}
        tasks.insert(0, task)
        return {'task': task}
    if action == 'plan':
        return {'sha256': '1'*64, 'inputs': {'custom_mod.yaml': '2'*64}, 'candidate_sha256': '3'*64,
                'candidate': 'synthetic test plan', 'notice': '合成测试，不执行实际服务应用'}
    raise ValueError('Unsupported synthetic operation')


def snapshot():
    result = fixture['snapshot']()
    result['components']['dnscrypt-socks'] = {'statename': 'STOPPED', 'pid': 0, 'start': 0, 'exitstatus': 0}
    result['dns'] = {'source': 'linux_process_sockets', 'scope': 'service_network_namespace', 'mode': 'automatic', 'entry_component': 'mosdns',
                     'client_path_verified': False, 'listeners': {
                        component: {'available': True, 'reason': None, 'pid': result['components'][component]['pid'], 'start_ticks': '100',
                          'endpoints': [{'address': address, 'port': port, 'transport': transport, 'family': family, 'wildcard': address == '::'} for transport in ('udp', 'tcp')]}
                        for component, address, port, family in [('mosdns', '::', 15453, 6), ('unbound-raw', '127.0.0.1', 5301, 4), ('unbound-forward', '127.0.0.1', 5304, 4)]}}
    return result


server.runtime_client.manager_request = management
server.runtime_client.snapshot = snapshot
httpd = fixture['make_server']('127.0.0.1', 0, server.app, threaded=True)
print(json.dumps({'port': httpd.server_port}), flush=True)
httpd.serve_forever()
