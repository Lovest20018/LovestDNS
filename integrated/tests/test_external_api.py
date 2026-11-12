"""Token access is opt-in and cannot change its own switch."""
import pytest
from browser_auth import external_api_enabled, set_external_api
from test_browser_auth import provision
from test_integrated_api import client, AUTH  # noqa: F401


@pytest.mark.parametrize('route', ['/api/v1/instance', '/api/v1/openapi.json', '/api/status', '/api/file/custom_env.ini'])
def test_external_api_is_off_for_new_state_and_blocks_both_route_families(client, route):
    api, _, state = client
    with __import__('browser_auth').database(state) as db:
        db.execute('DELETE FROM settings')
    assert not external_api_enabled(state)
    result = api.get(route, headers=AUTH)
    assert result.status_code == 403
    if route.startswith('/api/v1/'):
        assert result.get_json()['error']['code'] == 'API_DISABLED'


def test_browser_can_toggle_immediately_and_persist_without_token_access(client, monkeypatch):
    api, _, state = client
    monkeypatch.setenv('SUITE_READ_TOKEN', 'switch-test-observer')
    set_external_api(state, False)
    csrf = provision(client)
    assert api.get('/api/v1/capabilities').status_code == 200
    assert api.get('/api/v1/openapi.json').status_code == 200
    assert api.put('/api/v1/file/custom_env.ini', json={'content': 'CNAUTO="yes"\n', 'expected_revision': 'missing'},
                   headers={'X-CSRF-Token': csrf}).status_code == 200
    assert api.get('/auth/external-api').get_json() == {'enabled': False}
    assert api.put('/auth/external-api', json={'enabled': True}).status_code == 403
    assert api.put('/auth/external-api', json={'enabled': 'true'}, headers={'X-CSRF-Token': csrf}).status_code == 400
    assert api.put('/auth/external-api', json={'enabled': True}, headers={'X-CSRF-Token': csrf}).get_json() == {'enabled': True}
    assert external_api_enabled(state)
    with __import__('server').app.test_client() as other:
        assert other.get('/api/v1/instance', headers=AUTH).status_code == 200
        observer = {'Authorization': 'Bearer switch-test-observer'}
        assert other.get('/api/v1/instance', headers=observer).status_code == 200
        assert other.get('/api/v1/env', headers=observer).status_code == 403
        assert other.put('/auth/external-api', json={'enabled': False}, headers=AUTH).status_code == 401
    assert api.put('/auth/external-api', json={'enabled': False}, headers={'X-CSRF-Token': csrf}).status_code == 200
    assert api.get('/api/v1/instance', headers=AUTH).status_code == 403
    assert api.get('/api/status', headers=AUTH).status_code == 403
    assert api.get('/api/v1/capabilities').status_code == 200
    assert api.post('/api/v1/dns-test', json={}, headers={'X-CSRF-Token': csrf}).status_code == 400


def test_setting_rejects_cross_origin_and_initial_password(client):
    api, _, state = client
    set_external_api(state, False)
    csrf = provision(client)
    result = api.put('/auth/external-api', json={'enabled': True},
                     headers={'X-CSRF-Token': csrf, 'Origin': 'https://foreign.example'})
    assert result.status_code == 403 and not external_api_enabled(state)
    with __import__('browser_auth').database(state) as db:
        db.execute('UPDATE admin SET initial=1')
    assert api.get('/auth/external-api').status_code == 403
