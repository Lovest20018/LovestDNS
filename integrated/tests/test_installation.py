import hashlib
import json
import pytest
from installation import installation_summary, provenance_summary, read_object, receipt_summary


@pytest.mark.parametrize('name', ['lovestdns', 'lovestdns-validation'])
def test_native_maintenance_identity_uses_installer_path_not_display_name(monkeypatch, name):
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '1')
    monkeypatch.setenv('LOVESTDNS_HOME', f'/opt/{name}/current')
    monkeypatch.setenv('SUITE_INSTANCE_NAME', 'another label; sudo bad-command')
    result = installation_summary()
    assert result['service_name'] == name + '.service'
    assert result['control_command'] == f'/usr/local/sbin/{name}ctl'
    assert result['config_file'] == f'/etc/{name}/{name}.env'
    assert result['data_dir'] == f'/var/lib/{name}/data'


@pytest.mark.parametrize('home', ['', '/opt/lovestdns/current/../other', '/tmp/lovestdns/current',
                                 '/opt/lovestdns-x;id/current', '/opt/lovestdns-Upper/current'])
def test_unknown_native_identity_does_not_offer_default_instance_commands(monkeypatch, home):
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '1')
    monkeypatch.setenv('LOVESTDNS_HOME', home)
    assert installation_summary()['control_command'] is None
    monkeypatch.setenv('LOVESTDNS_SYSTEMD', '0')
    monkeypatch.setenv('LOVESTDNS_HOME', '/opt/lovestdns/current')
    assert installation_summary()['source'] == 'unavailable'


def test_app_frontend_base_and_upstream_provenance_are_separate(tmp_path):
    frontend = {'source_commit': 'f' * 40, 'source_snapshot': {'sha256': 'f' * 64},
                'dependency_mode': 'npm-ci', 'working_tree_modified': False}
    (tmp_path / 'build-manifest.json').write_text(json.dumps(frontend))
    info = {'source_commit': 'e' * 40, 'native_refresh': {'source_commit': 'a' * 40, 'source_sha256': 'a' * 64,
            'method': 'verified-components-with-current-app-ui', 'base_version': 'base.1', 'base_archive_sha256': 'b' * 64}}
    result = provenance_summary(info, tmp_path)
    assert result['application']['source_commit'] == 'a' * 40
    assert result['frontend']['source_commit'] == 'f' * 40
    assert result['base_package']['archive_sha256'] == 'b' * 64
    assert result['process_files_verified'] is False
    # Old packages cannot acquire an application commit from their upstream lock.
    assert provenance_summary({'source_commit': 'e' * 40}, tmp_path)['application']['source_commit'] is None
    (tmp_path / 'build-manifest.json').write_text('[]')
    assert provenance_summary({'native_refresh': []}, tmp_path)['frontend']['source_commit'] is None


def test_bad_or_oversized_metadata_does_not_break_service_information(tmp_path):
    path = tmp_path / 'metadata.json'
    for value in ('[1]', '{', '{"payload":"' + 'x' * (1024 * 1024) + '"}'):
        path.write_text(value)
        assert read_object(path) == {}
    assert read_object(tmp_path / 'missing.json') == {}


def test_receipt_must_bind_current_manifest_release_and_service(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    raw = b'{"version":"candidate.2"}'
    (release / 'manifest.json').write_bytes(raw)
    path = tmp_path / 'receipt.json'
    expected = {'format_version': 1, 'service_name': 'lovestdns-validation', 'release_path': str(release),
                'version': 'candidate.2', 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
                'archive_sha256': 'b' * 64, 'installed_at': 100.0}
    assert receipt_summary(release, path, 'lovestdns-validation')['status'] == 'missing'
    for field, value in [('manifest_sha256', 'c' * 64), ('release_path', '/old/release'), ('version', 'candidate.1'),
                         ('service_name', 'lovestdns'), ('archive_sha256', 'bad'), ('installed_at', float('nan'))]:
        path.write_text(json.dumps({**expected, field: value}))
        result = receipt_summary(release, path, 'lovestdns-validation')
        assert result['status'] == 'mismatch'
        assert result['archive_sha256'] is None
    path.write_text(json.dumps(expected))
    assert receipt_summary(release, path, 'lovestdns-validation')['archive_sha256'] == 'b' * 64
    # Receipt-less old releases and direct installs retain an honest unknown archive.
    path.write_text(json.dumps({**expected, 'archive_sha256': None}))
    assert receipt_summary(release, path, 'lovestdns-validation')['status'] == 'recorded'
    assert receipt_summary(release, path, 'lovestdns-validation')['archive_sha256'] is None
