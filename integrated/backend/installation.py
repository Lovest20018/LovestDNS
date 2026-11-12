"""Read bounded, non-secret installation records; never infer identity from a label."""
import hashlib
import json
import math
import os
from pathlib import Path
import re


def read_object(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def recorded(value, pattern=None):
    return value if isinstance(value, str) and 0 < len(value) <= 256 and (not pattern or re.fullmatch(pattern, value)) else None


def checksum(value):
    return recorded(value, r'[a-f0-9]{64}')


def provenance_summary(info, dist):
    app = info.get('native_refresh') or info.get('application')
    app = app if isinstance(app, dict) else {}
    frontend = read_object(Path(dist) / 'build-manifest.json')
    snapshot = frontend.get('source_snapshot')
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    try:
        if frontend:
            with (Path(dist) / 'build-manifest.json').open('rb') as stream:
                manifest_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        else:
            manifest_sha = None
    except OSError:
        manifest_sha = None
    return {
        'source': 'installed_metadata',
        'application': {'source_commit': recorded(app.get('source_commit'), r'[a-f0-9]{40,64}'),
                        'source_sha256': checksum(app.get('source_sha256')), 'method': recorded(app.get('method'))},
        'frontend': {'source_commit': recorded(frontend.get('source_commit'), r'[a-f0-9]{40,64}'),
                     'source_sha256': checksum(snapshot.get('sha256')), 'manifest_sha256': manifest_sha,
                     'dependency_mode': recorded(frontend.get('dependency_mode')),
                     'working_tree_modified': frontend.get('working_tree_modified') if type(frontend.get('working_tree_modified')) is bool else None},
        'base_package': {'version': recorded(app.get('base_version')), 'archive_sha256': checksum(app.get('base_archive_sha256'))},
        'process_files_verified': False,
    }


def receipt_summary(release, receipt_path, service_name):
    result = {'status': 'missing', 'archive_sha256': None, 'manifest_sha256': None, 'installed_at': None}
    try:
        raw = (release / 'manifest.json').read_bytes()
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            return result
        result['manifest_sha256'] = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return result
    receipt = read_object(receipt_path)
    if not receipt:
        return result
    result['status'] = 'mismatch'
    when = receipt.get('installed_at')
    archive = receipt.get('archive_sha256')
    if (receipt.get('format_version') != 1 or receipt.get('service_name') != service_name
            or receipt.get('release_path') != str(release) or receipt.get('version') != manifest.get('version')
            or receipt.get('manifest_sha256') != result['manifest_sha256']
            or (archive is not None and checksum(archive) is None)
            or type(when) not in (int, float) or not math.isfinite(when) or when < 0):
        return result
    result.update(status='recorded', archive_sha256=archive, installed_at=when)
    return result


def installation_summary():
    result = {'source': 'unavailable', 'service_name': None, 'control_command': None, 'config_file': None,
              'data_dir': None, 'state_dir': None, 'release_path': None,
              'receipt': {'status': 'missing', 'archive_sha256': None, 'manifest_sha256': None, 'installed_at': None}}
    # This is the installer's exact path convention, not SUITE_INSTANCE_NAME,
    # the browser URL, a hostname, or an arbitrary user-facing instance label.
    home = os.environ.get('LOVESTDNS_HOME', '')
    match = re.fullmatch(r'/opt/(lovestdns(?:-[a-z0-9][a-z0-9-]{0,30})?)/current', home)
    if os.environ.get('LOVESTDNS_SYSTEMD') != '1' or not match:
        return result
    name = match[1]
    result.update(source='native_service_environment', service_name=name + '.service',
                  control_command='/usr/local/sbin/' + name + 'ctl', config_file=f'/etc/{name}/{name}.env',
                  data_dir=f'/var/lib/{name}/data', state_dir=f'/var/lib/{name}/state')
    try:
        release = Path(home).resolve(strict=True)
        if release.parent != Path('/opt', name, 'releases').resolve(strict=True):
            return result
    except (OSError, RuntimeError):
        return result
    result['release_path'] = str(release)
    result['receipt'] = receipt_summary(release, Path('/opt', name, 'receipts', release.name + '.json'), name)
    return result
