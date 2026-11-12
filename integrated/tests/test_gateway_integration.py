import json
import pytest
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import BadRequest, BadGateway, Conflict
import gateway_integration as gw
from test_integrated_api import client, AUTH
from test_browser_auth import provision
from test_api_contract import validate

TOKEN = 'test-read-token-' + 'x' * 32
URL = 'http://127.0.0.1:18788/api/v1'

@pytest.mark.parametrize('url,expected', [('http://127.0.0.1/api/v1/', 'http://127.0.0.1:80/api/v1'), ('http://[::1]:18788/api/v1', 'http://[::1]:18788/api/v1'), ('https://1.1.1.1/api/v1', 'https://1.1.1.1:443/api/v1')])
def test_fixed_target_loopbacks(url, expected): assert gw.target(url) == expected

@pytest.mark.parametrize('url', ['http://example.com/api/v1', 'http://1.1.1.1/api/v1', 'http://127.0.0.1/other', 'http://x:y@127.0.0.1/api/v1', 'http://127.0.0.1/api/v1?x=1', 'http://169.254.169.254/api/v1', 'http://0.0.0.0/api/v1', 'http://[fe80::1]/api/v1'])
def test_reject_other_targets(url):
    with pytest.raises(BadRequest): gw.target(url)

def test_private_store_revision_and_credential_rules(tmp_path):
    store = gw.Store(tmp_path)
    assert store.public() == {'revision': 'missing', 'enabled': False, 'has_token': False}
    old = {**store.candidate({'url': URL, 'token': TOKEN}, {}), 'instance_id': 'gw-one', 'enabled': True}
    public = store.save(old, 'missing')
    assert TOKEN not in json.dumps(public) and 'token' not in public
    assert store.candidate({'url': URL, 'token': ''}, store.read())['instance_id'] == 'gw-one'
    with pytest.raises(BadRequest): store.candidate({'url': URL.replace('127.0.0.1', '127.0.0.2'), 'token': ''}, store.read())
    with pytest.raises(Conflict): store.save(old, 'missing')
    disconnected = store.save({'enabled': False}, public['revision'])
    assert not disconnected['has_token'] and 'token' not in store.read()

def sample(endpoint):
    data = {'instance': {'kind': 'paopaogateway', 'name': 'test', 'version': '1'}, 'capabilities': {'access_role': 'observer', 'instance_id': 'gw-one', 'mode': 'suburl', 'features': {'connection_history': True, 'persistent_history': True, 'mihomo_connection_snapshots': True, 'fakeip_lookup': True}}, 'runtime': {'core_responding': True, 'forwarding_verified': False, 'outbound_reachable': 'unknown', 'freshness': {'stale': False}}, 'mihomo/connections': {'items': [{'id': '1', 'metadata': {'host': 'example.com', 'sourceIP': '127.0.0.1', 'secret': TOKEN}, 'chains': ['DIRECT'], 'rule': 'MATCH', 'unknown': TOKEN}, {'id': '2', 'metadata': {'host': 'other.test'}}]}, 'connections': {'items': [], 'next_cursor': 'boot:1:signature'}, 'fakeip/lookup': {'status': 'no_records', 'source': 'actual_fakeip_cache', 'historical_ownership_verified': False}}
    return {'api_version': '1', 'instance_id': 'gw-one', 'data': data[endpoint], 'sampled_at': 100, 'fetched_at': 101}

def test_session_csrf_default_off_capabilities_filters_and_disconnect(client, monkeypatch):
    api, _, state = client
    calls = []
    def wire(config, endpoint, params=None): calls.append((endpoint, params)); return sample(endpoint)
    monkeypatch.setattr(gw, 'wire', wire)
    assert api.get('/api/v1/gateway/config', headers=AUTH).status_code == 403
    csrf = provision(client); headers = {'X-CSRF-Token': csrf}
    assert api.get('/api/v1/gateway/config').get_json()['data']['enabled'] is False
    assert api.get('/api/v1/gateway/summary').status_code == 409 and not calls
    body = {'url': URL, 'token': TOKEN, 'expected_revision': 'missing'}
    assert api.put('/api/v1/gateway/config', json=body).status_code == 403 and not calls
    assert api.post('/api/v1/gateway/test', json=body, headers=headers).status_code == 200
    assert not gw.Store(state).read()['enabled']
    saved = api.put('/api/v1/gateway/config', json=body, headers=headers)
    validate(saved, 'gateway_save')
    assert saved.status_code == 200 and TOKEN not in saved.get_data(as_text=True)
    revision = saved.get_json()['data']['revision']
    calls.clear()
    result = api.get('/api/v1/gateway/connections?view=mihomo&domain=example.com&client=127.0.0.1')
    assert result.status_code == 200
    validate(result, 'gateway_connections')
    assert calls[-1] == ('mihomo/connections', {'limit': 100})
    assert len(result.get_json()['data']['items']) == 1 and TOKEN not in result.get_data(as_text=True)
    assert api.get('/api/v1/gateway/fakeip?domain=').status_code == 400
    assert api.get('/api/v1/gateway/fakeip?domain=example.com').status_code == 200
    assert api.get('/api/v1/gateway/connections?view=history&cursor=bad').status_code == 400
    assert api.delete('/api/v1/gateway/config', json={'expected_revision': revision}, headers=headers).status_code == 200
    calls.clear()
    assert api.get('/api/v1/gateway/summary').status_code == 409 and not calls

def test_wire_protocol_bounds_identity_and_cursor(monkeypatch):
    class Response:
        status = 200
        data = json.dumps(sample('runtime')).encode()
        def getheader(self, key, default=None): return default
        def read1(self, size): value, self.data = self.data[:size], self.data[size:]; return value
        def read(self, size): return self.read1(size)
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path, headers): assert method == 'GET' and path.startswith('/api/v1/')
        def getresponse(self): return response
        def close(self): pass
    monkeypatch.setattr(gw.http.client, 'HTTPConnection', Connection)
    config = {'url': URL, 'token': TOKEN, 'instance_id': 'gw-one'}
    response = Response(); assert gw.wire(config, 'runtime')['instance_id'] == 'gw-one'
    response = Response(); response.data = json.dumps({**sample('runtime'), 'instance_id': 'changed'}).encode()
    with pytest.raises(Conflict): gw.wire(config, 'runtime')
    response = Response(); response.status = 302
    with pytest.raises(BadGateway): gw.wire(config, 'runtime')
    response = Response(); response.data = b'x' * (gw.LIMIT + 1)
    with pytest.raises(BadGateway): gw.wire(config, 'runtime')
    response = Response(); response.status = 409; response.data = b'{"error":{"code":"CURSOR_EXPIRED"}}'
    with pytest.raises(Conflict, match='CURSOR_EXPIRED'): gw.wire(config, 'connections')


def test_busy_config_write_returns_retryable_error_without_overwriting(client, monkeypatch):
    api, _, state = client
    csrf = provision(client)
    def busy(*_args, **_kwargs):
        raise gw.MaintenanceBusy('synthetic competing writer')
    monkeypatch.setattr(gw.Store, 'save', busy)
    result = api.delete('/api/v1/gateway/config', json={'expected_revision': 'missing'},
                        headers={'X-CSRF-Token': csrf})
    assert result.status_code == 503
    assert result.get_json()['error']['code'] == 'UNAVAILABLE'
    assert gw.Store(state).public()['enabled'] is False
