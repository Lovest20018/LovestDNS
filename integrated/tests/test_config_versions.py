from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest
import server
from test_integrated_api import client, AUTH


def test_two_clients_cannot_overwrite_same_version(client):
    api, data, _ = client
    (data / 'custom_env.ini').write_text('VALUE="original"\n')
    response = api.get('/api/v1/file/custom_env.ini', headers=AUTH)
    version = response.get_json()['data']['revision']
    assert response.headers['ETag'] == '"' + version + '"'
    barrier = threading.Barrier(2)
    def save(value):
        with server.app.test_client() as other:
            barrier.wait()
            return other.put('/api/v1/file/custom_env.ini', headers=AUTH,
                             json={'content': f'VALUE="{value}"\n', 'expected_revision': version}).status_code
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(save, ['first', 'second']))
    assert sorted(results) == [200, 412]
    assert len(api.get('/api/history/custom_env.ini', headers=AUTH).get_json()['revisions']) == 2


def test_create_headers_legacy_and_restore_conflict(client):
    api, data, _ = client
    route = '/api/v1/file/custom_env.ini'
    content = {'content': 'VALUE="first"\n'}
    assert api.put(route, headers={**AUTH, 'If-None-Match': '*'}, json=content).status_code == 200
    assert api.put(route, headers={**AUTH, 'If-None-Match': '*'}, json=content).status_code == 412
    response = api.get(route, headers=AUTH)
    etag = response.headers['ETag']
    assert api.put(route, headers={**AUTH, 'If-Match': 'W/' + etag}, json=content).status_code == 400
    assert api.put(route, headers={**AUTH, 'If-Match': etag}, json={'content': 'VALUE="second"\n'}).status_code == 200
    rev = api.get('/api/history/custom_env.ini', headers=AUTH).get_json()['revisions'][-1]['id']
    assert api.post(f'/api/v1/history/custom_env.ini/{rev}/restore', headers={**AUTH, 'If-Match': etag}).status_code == 412
    assert (data / 'custom_env.ini').read_text() == 'VALUE="second"\n'
    assert api.put(route, headers=AUTH, json=content).status_code == 200  # backwards compatibility


@pytest.mark.parametrize('host_edit', [False, True])
def test_save_reuses_original_bytes_and_refreshes_backup_after_host_edit(client, monkeypatch, host_edit):
    api, data, _ = client
    target = data / 'custom_env.ini'
    original, external = b'VALUE="old"\n', b'VALUE="host changed this"\n'
    target.write_bytes(original)
    reads = []
    read, prepare = server._configuration_bytes, server.history.prepare_write

    def track_read(filename, path):
        content = read(filename, path)
        reads.append(content)
        return content

    def prepare_with_host_edit(*args):
        prepare(*args)
        if host_edit:
            target.write_bytes(external)

    monkeypatch.setattr(server, '_configuration_bytes', track_read)
    monkeypatch.setattr(server.history, 'prepare_write', prepare_with_host_edit)
    candidate = 'VALUE="测试"\n'
    response = api.put('/api/v1/file/custom_env.ini', headers=AUTH, json={'content': candidate})
    assert response.status_code == 200
    payload = candidate.encode('utf-8')
    assert target.read_bytes() == payload
    assert target.with_suffix('.ini.bak').read_bytes() == (external if host_edit else original)
    assert reads == ([original, external, payload] if host_edit else [original, payload])


def test_save_preserves_external_target_of_backup_symlink(client, tmp_path, file_symlink):
    api, data, _ = client
    target = data / 'custom_env.ini'
    target.write_bytes(b'VALUE="original"\n')
    external = tmp_path / 'outside-data.ini'
    external.write_bytes(b'untouched')
    backup = target.with_suffix('.ini.bak')
    file_symlink(backup, external)

    candidate = 'VALUE="saved"\n'
    assert api.put('/api/v1/file/custom_env.ini', headers=AUTH, json={'content': candidate}).status_code == 200
    assert target.read_bytes() == candidate.encode('utf-8')
    assert backup.is_symlink() and backup.resolve() == external
    assert external.read_bytes() == b'untouched'


@pytest.mark.parametrize('backup_kind', ['directory', 'samefile', 'symlink'])
def test_save_skips_unsafe_backup_path(client, monkeypatch, backup_kind):
    api, data, _ = client
    target = data / 'custom_env.ini'
    target.write_bytes(b'VALUE="original"\n')
    backup = target.with_suffix('.ini.bak')
    marker = backup
    if backup_kind == 'directory':
        backup.mkdir()
        marker = backup / 'keep'
    elif backup_kind == 'samefile':
        samefile = Path.samefile
        monkeypatch.setattr(Path, 'samefile', lambda path, other: path == backup or samefile(path, other))
    else:
        is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda path: path == backup or is_symlink(path))
    marker.write_bytes(b'untouched')

    candidate = 'VALUE="saved"\n'
    assert api.put('/api/v1/file/custom_env.ini', headers=AUTH, json={'content': candidate}).status_code == 200
    assert target.read_bytes() == candidate.encode('utf-8')
    assert marker.read_bytes() == b'untouched'


def test_status_admin_only_and_not_verified(client, monkeypatch):
    api = client[0]
    monkeypatch.setenv('SUITE_READ_TOKEN', 'read-test')
    assert api.get('/api/v1/config/state', headers={'Authorization': 'Bearer read-test'}).status_code == 403
    monkeypatch.setattr(server.runtime_client, 'manager_request', lambda op: {'files': [], 'sampled_at': 1, 'scope': 'test', 'verified': False})
    assert api.get('/api/v1/config/state', headers=AUTH).get_json()['data']['verified'] is False


def test_oversized_existing_file_is_rejected_before_reading_or_history(client, monkeypatch):
    api, data, _ = client
    target = data / 'custom_env.ini'
    target.write_bytes(b'#' * (server.base.FILE_SIZE_LIMITS['custom_env.ini'] + 1))
    original = Path.read_bytes
    def reject_unbounded_read(path):
        assert path != target, 'Oversized configuration must not be read into memory'
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', reject_unbounded_read)
    response = api.put('/api/v1/file/custom_env.ini', headers=AUTH, json={'content': 'VALUE="safe"\n'})
    assert response.status_code == 500
    assert 'history size limit' in response.get_json()['error']['message']
    assert api.get('/api/v1/history/custom_env.ini', headers=AUTH).get_json()['data']['revisions'] == []
