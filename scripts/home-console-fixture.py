"""Only synthetic loopback data; never loads a real service or credentials."""
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import time
import uuid

root = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(root / 'scripts/ui-optimization-fixture.py'), run_name='home_fixture')
server, data = fixture['server'], fixture['data']
os.environ['SUITE_INSTANCE_NAME'] = 'LovestDNS Native Test — 用于验证长实例名称布局'
tasks = []
submissions = {}
controls = {'config_error': False, 'identity': 'named', 'lose_response': False, 'restart_seconds': 0,
            'manager_error': False, 'fail_next_restart': False, 'probe_failure': False,
            'drop_next_submission': False, 'reject_next_submission': False, 'last_request_id': None,
            'mixed_config': False}
from runtime.config_state import FILES
from runtime import software_update, update_network
from flask import jsonify, request

updates = server.STATE_DIR / 'updates'
updates.mkdir()
os.environ['LOVESTDNS_UPDATE_DIR'] = str(updates)
software_update.atomic_json(updates / 'protocol.json', {'schema_version': 1})
update_network.save(updates, {**update_network.DEFAULT, 'channel': 'preview'}, server.VERSION)


def seed_update(version):
    software_update.atomic_json(updates / 'status.json', {
        'phase': 'ready', 'checked_at': time.time(), 'release': {
            'version': version, 'archive_sha256': 'a' * 64, 'archive_bytes': 1024, 'prerelease': '-' in version,
            'notes': 'Synthetic browser fixture', 'published_at': '2026-11-18T00:00:00Z',
            'url': f'https://github.com/Lovest20018/LovestDNS/releases/tag/v{version}',
        },
    })


seed_update('9.9.9-rc.1')


@server.app.post('/fixture/home-control')
def home_control():
    body = request.get_json()
    if 'config_error' in body:
        controls['config_error'] = bool(body['config_error'])
    if body.get('identity') in ('named', 'unknown', 'mismatch'):
        controls['identity'] = body['identity']
    for name in ('lose_response', 'restart_seconds', 'manager_error', 'fail_next_restart', 'probe_failure',
                 'drop_next_submission', 'reject_next_submission', 'mixed_config'):
        if name in body:
            controls[name] = body[name]
    return jsonify({'ok': True, 'submissions': len(submissions), 'last_request_id': controls['last_request_id'],
                    'update_queued': (updates / 'request.json').exists()})


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
             'state': 'recorded' if tasks and (not controls['mixed_config'] or name == 'custom_mod.yaml') else 'unknown', 'recorded_revision': None,
             'recorded_at': tasks[0]['updated'] if tasks else None, 'action': tasks[0]['action'] if tasks else None, 'verified': False} for name in FILES]


def management(action, *args):
    now = time.time()
    for task in tasks:
        if task['state'] == 'running' and now >= task['created'] + controls['restart_seconds']:
            task.update(state='succeeded', updated=now, result={'message': '合成测试任务完成，未重启任何服务', 'verified': False})
    restarting = any(task['state'] == 'running' for task in tasks)
    if controls['manager_error']:
        raise RuntimeError('Synthetic manager connection failure')
    if action == 'capabilities':
        if restarting:
            return {'worker': {'available': False, 'phase': 'unavailable'}}
        return {'apply_custom_mod': True, 'save_redis_snapshot': True, 'restart_dns_core': True}
    if action == 'tasks':
        return {'tasks': tasks}
    if action == 'task':
        return {'task': submissions.get(args[0]) or next((task for task in tasks if task['id'] == args[0]), None)}
    if action == 'config_state':
        if controls['config_error']:
            raise RuntimeError('Synthetic configuration read failure')
        return {'files': versions(), 'scope': 'synthetic browser fixture', 'sampled_at': time.time(), 'verified': False}
    if action == 'submit':
        body = args[0]
        controls['last_request_id'] = body['request_id']
        existing = submissions.get(body['request_id'])
        if existing:
            return {'task': existing}
        if controls['drop_next_submission']:
            controls['drop_next_submission'] = False
            raise RuntimeError('Synthetic submission unavailable before acceptance')
        if controls['reject_next_submission']:
            controls['reject_next_submission'] = False
            raise ValueError('合成测试：重试被明确拒绝')
        task = {'id': uuid.uuid4().hex, 'action': body['action'], 'state': 'succeeded', 'created': now, 'updated': now,
                'result': {'message': '合成测试任务完成，未重启任何服务', 'verified': False}}
        if body['action'] == 'restart_dns_core':
            if controls['fail_next_restart']:
                controls['fail_next_restart'] = False
                task.update(state='failed', result={
                    'error': '合成测试：新配置初始化失败',
                    'rollback': {'runtime_restored': True, 'service_running': True},
                })
            elif controls['restart_seconds']:
                task.update(state='running', result=None)
        submissions[body['request_id']] = task
        tasks.insert(0, task)
        if controls['lose_response']:
            controls['lose_response'] = False
            time.sleep(0.45)
            raise RuntimeError('Synthetic lost submission response after acceptance')
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


def lookup(domain, *args, **kwargs):
    checked_at = datetime.now(timezone.utc).isoformat()
    result = fixture['lookup'](domain, *args, **kwargs)
    result.update(responded=True, checked_at=checked_at)
    if controls['probe_failure'] and domain == server.base.HEALTH_CHECK_DOMAINS['non_cn']:
        result.update(available=False, status='no_answer', rcode='SERVFAIL', answers=[], results=[])
    return result


server.base._dns_lookup = lookup
if __name__ == '__main__':
    httpd = fixture['make_server']('127.0.0.1', 0, server.app, threaded=True)
    print(json.dumps({'port': httpd.server_port}), flush=True)
    httpd.serve_forever()
