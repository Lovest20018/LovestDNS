"""Loopback browser fixture with synthetic records; never opens a packet socket."""
import json
from pathlib import Path
import runpy
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'scripts/ui-optimization-fixture.py'), run_name='query_fixture')
server = fixture['server']
from flask import jsonify, request
from runtime.query_recorder import Recorder
from runtime.filter_rules import parse
recorder = Recorder(server.STATE_DIR, server.RUN_DIR)
controls = {'unavailable': False, 'malformed': False}
filter_revision = 'a' * 64
filter_state = {'enabled': True, 'local_rules': '', 'sources': [], 'allow': {}, 'rules': 1,
                'revision': filter_revision, 'mode': 'mosdns', 'available': True, 'live_revision': filter_revision,
                'live_enabled': True, 'draft': None, 'jobs': []}
real_manager = server.runtime_client.manager_request


def manager(operation, payload=None):
    if operation == 'filter_status':
        return filter_state
    if operation == 'filter_check':
        return {'domain': payload, 'action': 'block', 'revision': filter_revision,
                'source': '合成订阅', 'id': 'a' * 24, 'rule': '||128.synthetic-query.example.test^'}
    if operation == 'filter_action':
        action = payload['action']
        if action == 'allow':
            filter_state['allow'][payload['domain']] = int(time.time()) + 600 if payload['mode'] == 'temporary' else None
        elif action == 'preview':
            _, parsed = parse('||ads.example.test^\n||skip.example.test^$dnstype=A')
            filter_state['draft'] = {'id': 'b' * 32, 'name': payload['name'], 'host': 'example.test', 'parse': parsed}
        elif action == 'apply_preview':
            assert payload['partial']
            draft = filter_state.pop('draft')
            filter_state['sources'] = [{**draft, 'enabled': True, 'interval': 86400, 'updated': int(time.time())}]
            filter_state['draft'] = None
        else:
            raise AssertionError(action)
        job = uuid.uuid4().hex
        filter_state['jobs'] = [{'id': job, 'action': action, 'phase': 'succeeded', 'message': '合成过滤任务已应用'}]
        return {'id': job}
    value = real_manager(operation, payload) if payload is not None else real_manager(operation)
    if operation == 'capabilities':
        value = {**value, 'dns_filter': True}
    return value


def records(payload):
    if controls['unavailable']:
        raise RuntimeError('合成采集服务不可用')
    if payload['operation'] == 'configure' and payload['enabled'] and not recorder.store.summary()['retained_rows']:
        # Deliberately synthetic documentation addresses. UI tests exercise the
        # real store, filters and API envelope; Linux wire evidence is separate.
        rows = []
        for index in range(130):
            rows.append({'id': f'synthetic-browser:{index}', 'observed_at': time.time() - 130 + index,
                'response_at': time.time() - 130 + index + .001, 'source_ip': f'192.0.2.{20 + index % 2}', 'source_port': 32123,
                'entry_address': '192.0.2.1', 'entry_port': 15453, 'component': 'mosdns',
                'transport': 'tcp' if index % 2 else 'udp', 'domain': f'{index}.synthetic-query.example.test.',
                'record_type': 'AAAA' if index % 2 else 'A', 'qtype': 28 if index % 2 else 1, 'qclass': 1,
                'same_key_packets': 1, 'rcode': 'NOERROR' if index % 3 else 'NXDOMAIN', 'rcode_value': 0 if index % 3 else 3,
                'answer_count': 1 if index % 3 else 0,
                'addresses': (['2001:db8::8'] if index % 2 else ['192.0.2.8']) if index % 3 else [],
                'addresses_complete': True, 'truncated': False, 'elapsed_ms': 1.25,
                'outcome': 'response_observed', 'timing_scope': 'passive_entry_packet_interval', 'branch': None, 'cache_hit': None})
        for index in range(4, 129, 4):
            rows[index].update(rcode='REFUSED', rcode_value=5, answer_count=0, addresses=[], timing_scope='mosdns_entry_processing',
                               filter={'action': 'block', 'revision': filter_revision, 'id': 'a' * 24,
                                       'source': '合成订阅', 'rule': f'||{index}.synthetic-query.example.test^'})
        recorder.store.append(rows[:128]); recorder.store.append(rows[128:])
        now = int(time.time())
        recorder.store.record_coverage(now - 600, now)
    if recorder.enabled:
        recorder.updated_at = time.time()
    value = recorder.request(payload)
    if payload['operation'] == 'configure':
        recorder.phase = 'recording' if recorder.enabled else 'disabled'
        value = recorder.status()
    if controls['malformed'] and payload['operation'] == 'recent':
        value['events'] = [{'id': 'malformed synthetic response'}]
    return value


server.runtime_client.query_records_request = records
server.runtime_client.manager_request = manager


@server.app.post('/fixture/query-control')
def query_control():
    body = request.get_json()
    for key in controls:
        if key in body: controls[key] = bool(body[key])
    return jsonify({'ok': True})


httpd = fixture['make_server']('127.0.0.1', 0, server.app, threaded=True)
print(json.dumps({'port': httpd.server_port}), flush=True)
httpd.serve_forever()
