import time
import struct

import pytest
from jsonschema import Draft202012Validator, FormatChecker
import server
from api_schema import openapi_document
from runtime.manager import Manager
from runtime.tasks import TaskStore
from test_integrated_api import client, AUTH
from test_maintenance import FakeCore


def validate(response, operation):
    document = openapi_document('test')
    endpoint = next(method for methods in document['paths'].values() for method in methods.values() if method['operationId'] == operation)
    schema = endpoint['responses'][str(response.status_code)]['content']['application/json']['schema']
    root = {**schema, 'components': document['components']}
    Draft202012Validator(root, format_checker=FormatChecker()).validate(response.get_json())


@pytest.fixture
def snapshot():
    return {'schema_version': 1, 'sampled_at': time.time(), 'interval_seconds': 5, 'ready': True, 'core_healthy': True,
            'components': {'unbound-raw': {'pid': 2, 'start': 100, 'statename': 'RUNNING', 'exitstatus': 0}},
            'layers': {'recursive': {'available': True, 'comparable': True, 'comparison_reason': 'comparable', 'window_seconds': 5.4,
                'qps': 0, 'cache_hit_ratio': None, 'counters': {'total.num.queries': 10, 'time.up': 20}},
                'forward': {'available': False, 'error': 'not running'}},
            'redis': {'available': False, 'error': 'missing'}, 'persistence': {'scope': 'Redis RDB', 'warnings': [], 'last_save_time': None}}


def test_schema_itself_and_all_documented_success_responses_have_concrete_types():
    document = openapi_document('test')
    for schema in document['components']['schemas'].values():
        Draft202012Validator.check_schema(schema)
    for methods in document['paths'].values():
        for method in methods.values():
            if method['operationId'] != 'schema':
                success = method['responses'].get('200', method['responses'].get('202'))
                assert '$ref' in success['content']['application/json']['schema']


def test_runtime_and_metrics_contracts_and_stale_data(client, monkeypatch, snapshot):
    api = client[0]
    monkeypatch.setattr(server.runtime_client, 'snapshot', lambda: snapshot)
    for operation in ('runtime', 'metrics'):
        response = api.get('/api/v1/' + operation, headers=AUTH)
        assert response.status_code == 200
        validate(response, operation)
        assert response.get_json()['data']['freshness']['max_age_seconds'] == 25
    metrics = api.get('/api/v1/metrics', headers=AUTH).get_json()['data']['metrics']
    assert metrics[0]['value'] == 0
    assert metrics[1]['value'] is None and metrics[1]['unavailable_reason'] == 'no_queries'
    assert metrics[0]['window_seconds'] == 5.4
    for offset in (-26, 60):
        snapshot['sampled_at'] = time.time() + offset
        response = api.get('/api/v1/runtime', headers=AUTH)
        assert response.status_code == 503
        assert response.get_json()['error']['code'] == 'STALE_DATA'
        validate(response, 'runtime')


def test_comparison_reasons_do_not_become_healthy_zeroes(client, monkeypatch, snapshot):
    monkeypatch.setattr(server.runtime_client, 'snapshot', lambda: snapshot)
    layer = snapshot['layers']['recursive']
    for reason in ('first_sample', 'process_changed', 'counters_reset', 'missing_counters'):
        layer.update(comparable=False, comparison_reason=reason, qps=None, cache_hit_ratio=None, window_seconds=None)
        response = client[0].get('/api/v1/metrics', headers=AUTH)
        validate(response, 'metrics')
        assert response.get_json()['data']['metrics'][0]['unavailable_reason'] == reason
        assert response.get_json()['data']['metrics'][0]['value'] is None


def test_config_history_save_restore_and_error_contracts(client):
    api, _, _ = client
    for method, path, body, operation in [
        ('get', '/instance', None, 'instance'), ('get', '/capabilities', None, 'capabilities'),
        ('put', '/file/custom_env.ini', {'content': 'LOCAL_IP="192.0.2.1"\n'}, 'file_write'),
        ('get', '/file/custom_env.ini', None, 'file_read'), ('get', '/status', None, 'status'),
        ('get', '/env', None, 'env'), ('get', '/history/custom_env.ini', None, 'history_list'),
        ('get', '/history/custom_env.ini/1', None, 'history_read'),
        ('post', '/history/custom_env.ini/1/restore', None, 'history_restore'),
        ('get', '/core-logs/redis', None, 'logs')]:
        response = getattr(api, method)('/api/v1' + path, json=body, headers=AUTH)
        assert response.status_code == 200, (path, response.get_json())
        validate(response, operation)
    response = api.put('/api/v1/file/custom_env.ini', json={'content': 'invalid'}, headers=AUTH)
    validate(response, 'file_write')
    response = api.delete('/api/v1/runtime', headers=AUTH)
    assert response.status_code == 405
    validate(response, 'runtime')


def test_dns_wire_comparison_and_health_match_the_contract(client, monkeypatch):
    def response(query, *args):
        return struct.pack('!HHHHHH', int.from_bytes(query[:2], 'big'), 0x8183, 1, 0, 0, 0) + query[12:]
    monkeypatch.setattr(server.base, '_exchange_dns', response)
    api = client[0]
    result = api.post('/api/v1/dns-test', json={'domain': 'example.com', 'record_type': 'TXT'}, headers=AUTH)
    validate(result, 'dns_query')
    result = api.post('/api/v1/dns-compare', json={'domain': 'example.com', 'record_type': 'HTTPS', 'targets': [{'label': 'core', 'server': '127.0.0.1', 'port': 53}, {'label': 'other', 'server': '127.0.0.1', 'port': 5301, 'transport': 'tcp'}]}, headers=AUTH)
    validate(result, 'dns_compare')
    assert len(result.get_json()['data']['tests']) == 2
    assert all(item['result']['rcode'] == 'NXDOMAIN' for item in result.get_json()['data']['tests'])
    validate(api.get('/api/v1/health-check', headers=AUTH), 'dns_health')


def test_task_lookup_retries_retention_and_recovery(client, monkeypatch, tmp_path):
    core = FakeCore(tmp_path / 'core')
    manager = Manager(core, TaskStore(core.state))
    monkeypatch.setattr(server.runtime_client, 'manager_request', lambda op, task=None: manager.dispatch({'operation': op, **({'task': task} if op in {'submit', 'task'} else {})}))
    api = client[0]
    validate(api.get('/api/v1/config/apply-plan', headers=AUTH), 'apply_plan')
    body = {'action': 'save_redis_snapshot', 'request_id': '1' * 32, 'expected_sha256': None}
    first = api.post('/api/v1/maintenance/tasks', json=body, headers=AUTH)
    validate(first, 'task_submit')
    task_id = first.get_json()['data']['task']['id']
    assert api.post('/api/v1/maintenance/tasks', json=body, headers=AUTH).get_json()['data']['task']['id'] == task_id
    manager.store.claim()
    manager.store.recover()
    result = api.get('/api/v1/maintenance/tasks/' + task_id, headers=AUTH)
    validate(result, 'task_read')
    assert result.get_json()['data']['task']['state'] == 'interrupted'
    assert api.post('/api/v1/maintenance/tasks', json=body, headers=AUTH).get_json()['data']['task']['state'] == 'interrupted'
    for i in range(25):
        task = manager.store.submit({**body, 'request_id': f'{i+2:032x}'})
        manager.store.claim(); manager.store.finish(task['id'], 'succeeded', {'state': 'saved', 'saved_at': time.time(), 'bytes': 100})
    assert task_id not in [t['id'] for t in manager.store.list()]
    assert api.get('/api/v1/maintenance/tasks/' + task_id, headers=AUTH).status_code == 200
    validate(api.get('/api/v1/maintenance/tasks', headers=AUTH), 'task_list')
    assert api.get('/api/v1/maintenance/tasks/' + 'f' * 32, headers=AUTH).status_code == 404
    assert api.get('/api/v1/maintenance/tasks/bad', headers=AUTH).status_code == 400


def test_observer_cannot_query_task_details(client, monkeypatch):
    monkeypatch.setenv('SUITE_READ_TOKEN', 'observer-token')
    response = client[0].get('/api/v1/maintenance/tasks/' + 'a' * 32, headers={'Authorization': 'Bearer observer-token'})
    assert response.status_code == 403
    validate(response, 'task_read')


def test_payload_limit_and_invalid_write_body_have_contract_errors(client):
    response = client[0].put('/api/v1/file/custom_env.ini', data='x' * (2 * 1024 * 1024 + 1),
                              content_type='application/json', headers=AUTH)
    assert response.status_code == 413
    validate(response, 'file_write')
    for content in ([], ['content'], 1, True):
        response = client[0].put('/api/v1/file/custom_env.ini', json=content, headers=AUTH)
        assert response.status_code == 400
        validate(response, 'file_write')
