"""Loopback-only synthetic fixture for test-ui-optimization.mjs; never a service entry point."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, required=True)
args = parser.parse_args()
fixture = args.root.resolve()
if not fixture.is_relative_to((ROOT / '.build').resolve()):
    raise SystemExit('Fixture state must stay within .build')
sys.path[:0] = [str(ROOT / 'integrated/backend'), str(ROOT / 'web/backend'), str(ROOT / 'integrated')]
mimetypes.add_type('application/javascript', '.js')
os.environ.update(DATA_DIR=str(fixture / 'data'), SUITE_STATE_DIR=str(fixture / 'state'),
                  SUITE_RUN_DIR=str(fixture / 'run'), SUITE_DIST_DIR=str(ROOT / '.build/integrated-web/dist'),
                  LOVESTDNS_SYSTEMD='1', WEB_UI_TOKEN='synthetic-browser-test-token')
import server
from browser_auth import reset_password, set_external_api, database
from flask import jsonify, request
from werkzeug.serving import make_server

data = Path(server.base.DATA_DIR)
data.mkdir(parents=True, exist_ok=True)
for name, text in {'custom_env.ini': '# preserve me\nCNAUTO="yes"\nLOCAL_IP="192.0.2.1"\n',
                   'custom_mod.yaml': '# preserve this comment\nHosts:\n- env_key: LOCAL_IP\n  zone: nas.example\n',
                   'unbound_custom.conf': 'server:\n', 'force_dnscrypt_list.txt': '# keep\n\ndomain:example.com\n',
                   'force_recurse_list.txt': '', 'force_forward_list.txt': '',
                   'force_ttl_rules.txt': '# custom explanation\nexample.com@@192.0.2.1\n'}.items():
    (data / name).write_text(text, encoding='utf-8', newline='\n')
reset_password(server.STATE_DIR, 'synthetic-browser-password')
set_external_api(server.STATE_DIR, False)
(server.STATE_DIR / 'logs').mkdir(exist_ok=True)
(server.STATE_DIR / 'logs/updater.log').write_text('synthetic updater diagnostic\n')
(server.STATE_DIR / 'logs/manager.log').write_text('synthetic manager diagnostic\n')
state = {'age': 0, 'fail_status': False, 'counts': {}}


def snapshot():
    return {'schema_version': 1, 'sampled_at': time.time() - state['age'], 'ready': True, 'core_healthy': True,
            'components': {**{name: {'statename': 'RUNNING', 'pid': 10 + i, 'start': 100, 'exitstatus': 0} for i, name in enumerate(['mosdns', 'unbound-raw', 'unbound-forward', 'redis', 'dnscrypt', 'watcher'])},
                           'updater': {'statename': 'EXITED', 'pid': 0, 'start': 100, 'exitstatus': 1}},
            'layers': {'recursive': {'available': True, 'comparable': True, 'qps': 1, 'cache_hit_ratio': 0.8}, 'forward': {'available': True, 'comparable': True, 'qps': 0, 'cache_hit_ratio': None}},
            'redis': {'available': True, 'info': {'used_memory': 1024, 'rdb_last_bgsave_status': 'ok'}}, 'persistence': {'warnings': [], 'scope': 'synthetic Redis RDB'}}


server.runtime_client.snapshot = snapshot
server.boot_env = lambda: {'CNAUTO': 'yes', 'RULES_TTL': '60', 'LOCAL_IP': '192.0.2.1'}
server.base._runtime_env = server.boot_env
server.build_info = lambda: {'image': 'synthetic-fixture', 'source_commit': 'synthetic-fixture'}


def management(action, *args):
    if action == 'capabilities':
        return {'apply_custom_mod': True, 'save_redis_snapshot': True, 'restart_dns_core': True}
    if action == 'tasks':
        return {'tasks': []}
    if action == 'config_state':
        return {'files': [], 'scope': 'synthetic', 'sampled_at': time.time(), 'verified': False}
    raise ValueError('Synthetic fixture does not execute maintenance')


server.runtime_client.manager_request = management


def lookup(domain, record_type='A', dns_server='127.0.0.1', port=53, transport='udp'):
    time.sleep(0.6)
    return {'domain': domain, 'record_type': record_type, 'server': dns_server, 'port': port, 'available': True,
            'status': 'ok', 'rcode': 'NOERROR', 'transport': transport, 'elapsed_ms': 1,
            'answers': [{'name': domain, 'type': record_type, 'value': '192.0.2.2', 'ttl': 60}], 'results': ['192.0.2.2']}


server.base._dns_lookup = lookup


@server.app.before_request
def fault_injection():
    state['counts'][request.path] = state['counts'].get(request.path, 0) + 1
    if state['fail_status'] and request.path == '/api/v1/status':
        return jsonify({'error': 'synthetic status unavailable'}), 503


@server.app.post('/fixture/control')
def control():
    body = request.get_json()
    if body.get('expire'):
        with database(server.STATE_DIR) as db:
            db.execute('DELETE FROM sessions')
    if 'age' in body:
        state['age'] = body['age']
    if 'fail_status' in body:
        state['fail_status'] = body['fail_status']
    if body.get('external_change'):
        (data / 'custom_env.ini').write_text('VALUE="external"\n', newline='\n')
    return jsonify({'ok': True})


@server.app.get('/fixture/counts')
def counts():
    return jsonify(state['counts'])


if __name__ == '__main__':
    httpd = make_server('127.0.0.1', 0, server.app, threaded=True)
    print(json.dumps({'port': httpd.server_port}), flush=True)
    httpd.serve_forever()
