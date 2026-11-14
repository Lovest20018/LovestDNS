"""Management authentication, fixed release source and durable queue boundaries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import time
import urllib.request

import pytest
import server
from browser_auth import COOKIE
from runtime import software_update as update
from test_browser_auth import provision
from test_integrated_api import client, AUTH


@pytest.fixture
def updates(client, monkeypatch):
    root = client[2].parent / 'updates'
    root.mkdir()
    update.atomic_json(root / 'protocol.json', {'schema_version': 1})
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '1')
    monkeypatch.setenv('LOVESTDNS_UPDATE_DIR', str(root))
    return root


def checked(root):
    release = {'version': 'test.next', 'archive_sha256': 'a' * 64, 'archive_bytes': 1234,
               'prerelease': True, 'published_at': '2026-01-01T00:00:00Z',
               'url': 'https://github.com/Lovest20018/LovestDNS/releases/tag/vtest.next', 'notes': 'Test release'}
    update.atomic_json(root / 'status.json', {'phase': 'ready', 'checked_at': time.time(), 'release': release})
    return {'action': 'apply', 'version': release['version'], 'archive_sha256': release['archive_sha256']}


def test_updates_require_changed_password_browser_session_not_api_tokens(client, updates):
    api = client[0]
    for method in ('get', 'post'):
        response = getattr(api, method)('/auth/software-update', headers=AUTH, json={'action': 'check'})
        assert response.status_code == 401
    assert api.get('/auth/session').status_code == 200
    initial = (client[2] / 'admin-password.initial').read_text().strip()
    session = api.post('/auth/login', json={'password': initial}).get_json()
    assert api.get('/auth/software-update').status_code == 403
    assert api.post('/auth/software-update', json={'action': 'check'},
                    headers={'X-CSRF-Token': session['csrf_token']}).status_code == 403
    assert not (updates / 'request.json').exists()


@pytest.mark.parametrize('headers', [{}, {'X-CSRF-Token': 'wrong'},
    {'Origin': 'https://cross-site.example'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_update_submission_rejects_missing_csrf_and_cross_origin(client, updates, headers):
    csrf = provision(client)
    sent = {'X-CSRF-Token': csrf, **headers} if headers.get('Origin') or headers.get('Sec-Fetch-Site') else headers
    result = client[0].post('/auth/software-update', json={'action': 'check'}, headers=sent)
    assert result.status_code == 403
    assert not (updates / 'request.json').exists()


def test_browser_check_is_durable_and_duplicate_click_cannot_start_another(client, updates):
    csrf = provision(client)
    api = client[0]
    result = api.post('/auth/software-update', json={'action': 'check'}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 202 and result.headers['Cache-Control'] == 'no-store'
    job = result.get_json()
    assert job['phase'] == 'queued' and job['action'] == 'check'
    assert update.read_json(updates / 'request.json')['job_id'] == job['job_id']
    assert api.post('/auth/software-update', json={'action': 'check'}, headers={'X-CSRF-Token': csrf}).status_code == 409
    assert api.get('/auth/software-update').get_json()['job_id'] == job['job_id']


@pytest.mark.parametrize('payload', [
    {'action': 'apply', 'version': 'test.next', 'archive_sha256': 'a' * 64},
    {'action': 'check', 'url': 'https://other.example/install.py'},
    {'action': 'check', 'instance': 'another-instance'},
    {'action': 'shell', 'command': 'anything'}, [], None,
])
def test_client_cannot_select_unchecked_package_url_command_or_instance(client, updates, payload):
    csrf = provision(client)
    result = client[0].post('/auth/software-update', json=payload, headers={'X-CSRF-Token': csrf})
    assert result.status_code in {400, 415}
    assert not (updates / 'request.json').exists()


def test_apply_is_pinned_to_fresh_check_and_blocks_config_writes_only_during_upgrade(client, updates):
    csrf = provision(client)
    api, data, _ = client
    payload = checked(updates)
    changed = {**payload, 'archive_sha256': 'b' * 64}
    assert api.post('/auth/software-update', json=changed, headers={'X-CSRF-Token': csrf}).status_code == 400
    result = api.post('/auth/software-update', json=payload, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 202
    blocked = api.put('/api/v1/file/custom_env.ini', json={'content': 'CNAUTO="no"\n'}, headers={'X-CSRF-Token': csrf})
    assert blocked.status_code == 503 and blocked.get_json()['error']['code'] == 'UNAVAILABLE'
    assert not (data / 'custom_env.ini').exists()
    assert api.put('/auth/external-api', json={'enabled': False}, headers={'X-CSRF-Token': csrf}).status_code == 503
    assert api.get('/api/v1/capabilities').status_code == 200
    assert api.get('/auth/software-update').get_json()['job_id'] == result.get_json()['job_id']
    status = update.read_json(updates / 'status.json')
    update.atomic_json(updates / 'status.json', {**status, 'phase': 'failed'})
    assert api.put('/api/v1/file/custom_env.ini', json={'content': 'CNAUTO="no"\n'}, headers={'X-CSRF-Token': csrf}).status_code == 200


def test_expired_check_cannot_be_applied(updates):
    payload = checked(updates)
    status = update.read_json(updates / 'status.json')
    update.atomic_json(updates / 'status.json', {**status, 'checked_at': time.time() - 3601})
    with pytest.raises(update.UpdateError, match='重新检查'):
        update.queue(updates, payload, 'test.old')


def test_cross_worker_queue_admits_one_job(updates):
    def submit(_):
        try:
            return update.queue(updates, {'action': 'check'}, 'test.old')['job_id']
        except update.UpdateBusy:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    accepted = [value for value in results if value]
    assert len(accepted) == 1
    assert update.read_json(updates / 'request.json')['job_id'] == accepted[0]


def test_status_uses_running_version_after_service_restart(updates):
    update.atomic_json(updates / 'status.json', {'phase': 'succeeded', 'current_version': 'test.old'})
    assert update.status(updates, 'test.new')['current_version'] == 'test.new'


def test_container_deployment_has_no_update_queue(client, monkeypatch):
    provision(client)
    monkeypatch.delenv('LOVESTDNS_SYSTEMD', raising=False)
    result = client[0].get('/auth/software-update').get_json()
    assert result['supported'] is False


def release(version, published, **changes):
    base = f'https://github.com/{update.REPOSITORY}/releases/download/v{version}/'
    return {'tag_name': 'v' + version, 'draft': False, 'prerelease': True, 'published_at': published,
            'body': 'Release notes', 'assets': [{'name': name, 'size': 103 if name.endswith('.sha256') else 2000,
              'browser_download_url': base + name, 'digest': None} for name in (update.ASSET, update.ASSET + '.sha256')], **changes}


def test_latest_native_release_includes_prereleases_and_ignores_incomplete_or_draft(monkeypatch):
    values = [release('test.old', '2026-01-01T00:00:00Z'), release('test.new', '2026-02-01T00:00:00Z'),
              release('test.draft', '2026-04-01T00:00:00Z', draft=True),
              release('test.incomplete', '2026-05-01T00:00:00Z', assets=[])]
    urls = []
    def fetch(url, limit):
        urls.append(url)
        return (('a' * 64 + '  ' + update.ASSET + '\n').encode()
                if url.endswith('.sha256') else json.dumps(values).encode())
    monkeypatch.setattr(update, 'public_bytes', fetch)
    result = update.checked_release(channel='preview')
    assert result['version'] == 'test.new' and result['prerelease'] is True and result['archive_sha256'] == 'a' * 64
    assert urls[1] == f'https://github.com/{update.REPOSITORY}/releases/download/vtest.new/{update.ASSET}.sha256'


@pytest.mark.parametrize('fault', ['url', 'asset', 'duplicate', 'size', 'version'])
def test_invalid_release_metadata_never_becomes_an_install_target(fault):
    value = copy.deepcopy(release('test.new', '2026-02-01T00:00:00Z'))
    if fault == 'url': value['assets'][0]['browser_download_url'] = 'https://other.example/' + update.ASSET
    if fault == 'asset': value['assets'][0]['name'] = 'install.sh'
    if fault == 'duplicate': value['assets'].append(value['assets'][0])
    if fault == 'size': value['assets'][0]['size'] = update.MAX_ARCHIVE + 1
    if fault == 'version': value['tag_name'] = 'v../escape'
    assert update.release_summary(value) is None


def test_release_checksum_must_match_github_asset_digest(monkeypatch):
    value = release('test.new', '2026-02-01T00:00:00Z')
    value['assets'][0]['digest'] = 'sha256:' + 'b' * 64
    monkeypatch.setattr(update, 'public_bytes', lambda url, limit: (
        ('a' * 64 + '  ' + update.ASSET).encode() if url.endswith('.sha256') else json.dumps(value).encode()))
    with pytest.raises(update.UpdateError, match='摘要'):
        update.checked_release('test.new', channel='preview')


def test_metadata_and_checksum_share_explicit_update_transport(monkeypatch):
    marker, seen = object(), []
    def fetch(url, limit, *, opener):
        seen.append(opener)
        return (('a' * 64 + '  ' + update.ASSET).encode() if url.endswith('.sha256')
                else json.dumps(release('test.new', '2026-02-01T00:00:00Z', prerelease=False)).encode())
    monkeypatch.setattr(update, 'public_bytes', fetch)
    assert update.checked_release(opener=marker)['version'] == 'test.new'
    assert seen == [marker, marker]


def test_stable_channel_uses_latest_official_release_and_never_accepts_prerelease(monkeypatch):
    urls = []
    value = release('0.1.0', '2026-02-01T00:00:00Z', prerelease=False)
    def fetch(url, limit):
        urls.append(url)
        return (('a' * 64 + '  ' + update.ASSET).encode() if url.endswith('.sha256')
                else json.dumps(value).encode())
    monkeypatch.setattr(update, 'public_bytes', fetch)
    assert update.checked_release()['version'] == '0.1.0'
    assert urls[0].endswith('/releases/latest')
    value['prerelease'] = True
    with pytest.raises(update.UpdateError, match='正式版'):
        update.checked_release()
    with pytest.raises(update.UpdateError, match='正式版'):
        update.checked_release('0.1.0')


@pytest.mark.parametrize('url', ['http://github.com/file', 'https://other.example/file',
    'https://github.com:444/file', 'https://user:password@github.com/file'])
def test_redirect_never_downgrades_tls_or_leaves_github(url):
    with pytest.raises(update.UpdateError):
        update.GitHubRedirect().redirect_request(urllib.request.Request('https://github.com/file'), None, 302, '', {}, url)
