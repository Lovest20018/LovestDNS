"""Loopback browser fixture for unified console navigation."""
import json
from pathlib import Path
import runpy
import time

from flask import jsonify, request

root = Path(__file__).resolve().parents[1]
home = runpy.run_path(str(root / 'scripts/home-console-fixture.py'))
server = home['server']
home['update_network'].save(home['updates'], home['update_network'].DEFAULT, server.VERSION)
home['seed_update']('0.2.2')
GATEWAY_URL = 'https://192.0.2.100:18788/api/v1'
state = {'gateway': True, 'gateway_revision': 1, 'role': 'admin', 'deployment': 'native', 'filter_refreshes': 0}


@server.app.post('/fixture/navigation')
def navigation_control():
    values = request.get_json()
    for key in ('gateway', 'role', 'deployment'):
        if key in values:
            state[key] = values[key]
    return jsonify({'ok': True})


@server.app.get('/fixture/navigation-state')
def navigation_state():
    settings = home['update_network'].load(home['updates'])
    return jsonify({'filter_refreshes': state['filter_refreshes'], 'network': {key: settings[key] for key in ('channel', 'mode', 'host', 'port')}})


@server.app.before_request
def synthetic_gateway():
    path = request.path
    if path.startswith('/api/v1/filter') and request.method != 'GET':
        state['filter_refreshes'] += 1
    now = time.time()
    summary = {'instance': {'id': 'gateway-synthetic', 'name': 'Synthetic Gateway'}, 'features': {'connection_history': True, 'fakeip_lookup': True},
               'mode': 'direct', 'runtime': {'status': 'ready'}, 'sampled_at': now, 'fetched_at': now, 'freshness': {'stale': False}}
    if path == '/api/v1/gateway/config':
        revision = f"synthetic-{state['gateway_revision']}"
        if request.method in ('PUT', 'DELETE'):
            body = request.get_json()
            if body.get('expected_revision') != revision:
                return jsonify({'error': {'code': 'CONFLICT', 'message': '联动配置已改变，请重新读取'}}), 409
            if request.method == 'PUT' and (body.get('url') != GATEWAY_URL or not (body.get('token') or state['gateway'])):
                return jsonify({'error': {'code': 'INVALID_ARGUMENT', 'message': '请填写测试网关地址与只读 Token'}}), 400
            state['gateway'] = request.method == 'PUT'
            state['gateway_revision'] += 1
            revision = f"synthetic-{state['gateway_revision']}"
        config = {'enabled': state['gateway'], 'revision': revision, 'has_token': state['gateway']}
        if state['gateway']:
            config.update(url=GATEWAY_URL, name='Synthetic Gateway')
        return jsonify({'data': config, 'meta': {'api_version': '1'}})
    record = {
        'id': 'synthetic-connection-85', 'source': 'sniffbox_lifecycle', 'domain': 'example.com',
        'client': '192.0.2.20', 'destination': '192.0.2.21', 'upstream_peer': '192.0.2.22:1080',
        'state': 'closed', 'started_at': now - 300, 'chains': [],
        'decision': {'application_result': 'unknown', 'connect_result': 'connected', 'outbound_id': None,
                     'reason': 'mihomo_decision_unavailable', 'rule_id': 'mode:suburl', 'rule_kind': 'mode_dispatch',
                     'source': 'sniffbox_outbound_dispatch', 'status': 'unavailable'},
    }
    data = {
        '/api/v1/gateway/test': summary,
        '/api/v1/gateway/summary': summary,
        '/api/v1/gateway/connections': {'items': [record], 'view': 'recent', 'sampled_at': now, 'fetched_at': now, 'freshness': {'stale': False},
                                       'scope': 'synthetic', 'filter_scope': 'exact', 'complete_history': False},
        '/api/v1/gateway/fakeip': {'status': 'matched', 'domain': 'example.com', 'fakeip': '198.18.0.3', 'sampled_at': now, 'fetched_at': now},
    }.get(path)
    if data is not None:
        return jsonify({'data': data, 'meta': {'api_version': '1'}})


@server.app.after_request
def alter_capabilities(response):
    if request.path == '/api/v1/capabilities' and response.is_json and response.status_code == 200:
        body = response.get_json()
        caps = body['data']
        caps['access_role'] = state['role']
        caps['deployment'] = state['deployment']
        caps['features']['dns_filter'] = True
        if state['role'] == 'observer':
            caps['permissions'].update(configure=False, maintain=False)
        return jsonify(body)
    return response


if __name__ == '__main__':
    httpd = home['fixture']['make_server']('127.0.0.1', 0, server.app, threaded=True)
    print(json.dumps({'port': httpd.server_port}), flush=True)
    httpd.serve_forever()
