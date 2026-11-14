"""Proxy credentials, release confirmation and browser-only configuration."""
import os
import stat

import pytest
from runtime import software_update as protocol
from runtime import update_network as network
from test_browser_auth import provision
from test_integrated_api import client, AUTH
from test_software_update import checked, updates

PROFILE = {'mode': 'socks5', 'host': '192.0.2.10', 'port': 1080,
           'username': 'fixture-user', 'password': 'synthetic-secret', 'channel': 'stable'}
URL = '/auth/software-update/network'


def test_network_settings_require_browser_session_and_changed_initial_password(client, updates):
    api = client[0]
    for method in ('get', 'put'):
        assert getattr(api, method)(URL, json=PROFILE, headers=AUTH).status_code == 401
    api.get('/auth/session')
    initial = (client[2] / 'admin-password.initial').read_text().strip()
    csrf = api.post('/auth/login', json={'password': initial}).get_json()['csrf_token']
    assert api.get(URL).status_code == 403
    assert api.put(URL, json=PROFILE, headers={'X-CSRF-Token': csrf}).status_code == 403


@pytest.mark.parametrize('headers', [{}, {'X-CSRF-Token': 'wrong'}, {'Origin': 'https://foreign.example'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_network_writes_require_csrf_and_same_origin(client, updates, headers):
    csrf = provision(client)
    sent = {'X-CSRF-Token': csrf, **headers} if 'Origin' in headers or 'Sec-Fetch-Site' in headers else headers
    assert client[0].put(URL, json=PROFILE, headers=sent).status_code == 403
    assert not (updates / 'network.json').exists()


def test_secret_is_private_never_returned_and_old_confirmation_is_invalidated(client, updates):
    csrf = provision(client)
    body = checked(updates)
    result = client[0].put(URL, json=PROFILE, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200
    assert 'password' not in result.get_json() and PROFILE['password'] not in result.get_data(as_text=True)
    assert result.get_json()['password_configured'] is True
    assert client[0].get(URL).get_json() == result.get_json()
    assert result.headers['Cache-Control'] == 'no-store'
    assert network.load(updates) == PROFILE
    if os.name != 'nt':
        assert stat.S_IMODE((updates / 'network.json').stat().st_mode) == 0o600
    with pytest.raises(protocol.UpdateError, match='重新检查'):
        protocol.queue(updates, body, 'test.old')


def test_empty_password_retains_only_same_identity_and_direct_clears_credentials(updates):
    network.save(updates, PROFILE, 'test.old')
    network.save(updates, {**PROFILE, 'password': ''}, 'test.old')
    assert network.load(updates)['password'] == PROFILE['password']
    with pytest.raises(protocol.UpdateError, match='重新填写密码'):
        network.save(updates, {**PROFILE, 'host': '192.0.2.11', 'password': ''}, 'test.old')
    assert network.load(updates) == PROFILE
    network.save(updates, PROFILE, 'test.old')
    network.save(updates, {**PROFILE, 'password': '', 'clear_password': True}, 'test.old')
    assert network.load(updates)['password'] == ''
    assert network.load(updates)['username'] == ''
    network.save(updates, PROFILE, 'test.old')
    network.save(updates, {**PROFILE, 'mode': 'direct'}, 'test.old')
    assert network.load(updates) == network.DEFAULT


def test_network_cannot_change_during_check_or_install(updates):
    for phase in protocol.BUSY:
        protocol.atomic_json(updates / 'status.json', {'phase': phase})
        with pytest.raises(protocol.UpdateBusy):
            network.save(updates, PROFILE, 'test.old')
    assert not (updates / 'network.json').exists()


def test_legacy_proxy_defaults_to_stable_and_channel_change_invalidates_check(updates):
    legacy = {key: value for key, value in PROFILE.items() if key != 'channel'}
    protocol.atomic_json(updates / 'network.json', legacy)
    assert network.load(updates) == PROFILE
    checked(updates)
    changed = network.save(updates, {**legacy, 'password': '', 'channel': 'preview'}, '0.1.0')
    assert changed['channel'] == 'preview' and changed['password_configured'] is True
    assert protocol.status(updates, '0.1.0')['release'] is None
    assert network.load(updates)['password'] == PROFILE['password']
    changed = network.save(updates, {**network.DEFAULT, 'channel': 'preview'}, '0.1.0')
    assert changed['channel'] == 'preview' and changed['mode'] == 'direct'


@pytest.mark.parametrize('changes', [
    {'mode': []}, {'mode': 'system'}, {'channel': 'nightly'}, {'channel': []}, {'host': 'github.com'}, {'host': 123}, {'host': '0.0.0.0'},
    {'host': 'fe80::1'}, {'host': 'ff02::1'}, {'port': True}, {'port': 0},
    {'port': 65536}, {'username': 'header\r\ninjected'}, {'username': 'user:name'},
    {'password': 'a' * 256}, {'password': 'secret\x00'}, {'arbitrary_url': 'https://foreign.example'},
])
def test_invalid_profiles_cannot_modify_settings_or_confirmation(updates, changes):
    body = checked(updates)
    with pytest.raises(protocol.UpdateError):
        network.save(updates, {**PROFILE, **changes}, 'test.old')
    assert not (updates / 'network.json').exists()
    assert protocol.status(updates, 'test.old')['release']['version'] == body['version']
