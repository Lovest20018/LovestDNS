"""Bounded, host-local protocol for the independent native update worker."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'Lovest20018/LovestDNS'
ASSET = 'lovestdns-native-debian-amd64.tar.gz'
MAX_ARCHIVE = 256 * 1024 * 1024
BUSY = {'queued', 'checking', 'downloading', 'verifying', 'installing'}
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}')
DIGEST = re.compile(r'[a-f0-9]{64}')


class UpdateError(ValueError):
    pass


class UpdateBusy(UpdateError):
    pass


def read_json(path, default=None):
    path = Path(path)
    if path.is_symlink():
        raise UpdateError('更新记录路径无效')
    try:
        with path.open('rb') as source:
            payload = source.read(64 * 1024 + 1)
    except FileNotFoundError:
        return default
    if len(payload) > 64 * 1024:
        raise UpdateError('更新记录超过大小限制')
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise UpdateError('更新记录格式无效')
    return value


def atomic_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(value, target, ensure_ascii=False)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def protocol_lock(root):
    path = Path(root) / 'request-lock.sqlite'
    if path.is_symlink():
        raise UpdateError('更新锁路径无效')
    db = sqlite3.connect(path, timeout=2)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)')
        db.execute('BEGIN IMMEDIATE')
        yield
        db.commit()
    except sqlite3.OperationalError as exc:
        raise UpdateBusy('已有更新操作进行中，请稍后重新读取状态') from exc
    finally:
        db.close()


def supported(root):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return False
    return (read_json(root / 'protocol.json', {}) or {}).get('schema_version') == 1


def status(root, current_version):
    value = read_json(Path(root) / 'status.json', {})
    return {'schema_version': 1, 'supported': True, 'current_version': current_version,
            'phase': 'idle', 'job_id': None, 'action': None, 'release': None,
            'checked_at': None, 'message': '', **value, 'current_version': current_version}


def queue(root, body, current_version):
    root = Path(root)
    if not supported(root):
        raise UpdateError('当前安装尚未启用网页更新，请先用安装包升级一次')
    if not isinstance(body, dict) or body.get('action') not in {'check', 'apply'}:
        raise UpdateError('更新操作无效')
    action = body['action']
    fields = {'action'} if action == 'check' else {'action', 'version', 'archive_sha256'}
    if set(body) != fields:
        raise UpdateError('更新参数无效')
    with protocol_lock(root):
        previous = status(root, current_version)
        if previous['phase'] in BUSY or (root / 'request.json').exists():
            raise UpdateBusy('已有更新操作进行中，请等待完成')
        if action == 'apply':
            release = previous.get('release') or {}
            if (not isinstance(body['version'], str) or not VERSION.fullmatch(body['version'])
                    or not isinstance(body['archive_sha256'], str) or not DIGEST.fullmatch(body['archive_sha256'])
                    or release.get('version') != body['version']
                    or release.get('archive_sha256') != body['archive_sha256']
                    or body['version'] == current_version or not previous.get('checked_at')
                    or time.time() - previous['checked_at'] > 3600):
                raise UpdateError('请先重新检查更新，再确认已校验的新版本')
        job = {'job_id': secrets.token_hex(16), 'action': action, 'created_at': time.time(),
               'previous_version': current_version}
        if action == 'apply':
            job.update(version=body['version'], archive_sha256=body['archive_sha256'])
        queued = {**previous, **job, 'phase': 'queued', 'finished_at': None,
                  'message': '等待主机检查更新' if action == 'check' else '更新任务已提交，正在准备'}
        # Readers and the worker share this lock. Publish the complete job
        # atomically; systemd's PathExists also handles jobs queued at shutdown.
        atomic_json(root / 'status.json', queued)
        try:
            atomic_json(root / 'request.json', job)
        except BaseException:
            atomic_json(root / 'status.json', previous)
            raise
        return queued


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if (target.scheme != 'https' or target.hostname not in {
                'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                'objects.githubusercontent.com'} or target.port not in {None, 443}
                or target.username or target.password):
            raise UpdateError('GitHub 下载重定向地址无效')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def public_bytes(url, limit, *, opener=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'api.github.com', 'github.com'}
            or parsed.port not in {None, 443} or parsed.username or parsed.password):
        raise UpdateError('更新来源地址无效')
    request = urllib.request.Request(url, headers={'User-Agent': 'LovestDNS-native-updater',
        'Accept': 'application/vnd.github+json' if parsed.hostname == 'api.github.com' else 'application/octet-stream'})
    try:
        transport = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), GitHubRedirect())
        deadline = time.monotonic() + 60
        with transport.open(request, timeout=20) as response:
            data = bytearray()
            while True:
                chunk = response.read1(min(64 * 1024, limit + 1 - len(data)))
                if time.monotonic() > deadline:
                    raise TimeoutError('Update metadata deadline exceeded')
                data.extend(chunk)
                if not chunk or len(data) > limit:
                    break
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError('无法从 GitHub 获取更新，请检查更新专用代理或主机网络后重试') from exc
    if len(data) > limit:
        raise UpdateError('GitHub 更新信息超过大小限制')
    return bytes(data)


def release_summary(value):
    if not isinstance(value, dict) or value.get('draft') is not False:
        return None
    tag = value.get('tag_name')
    version = tag[1:] if isinstance(tag, str) and tag.startswith('v') else ''
    if not VERSION.fullmatch(version) or not isinstance(value.get('published_at'), str):
        return None
    assets = value.get('assets')
    if not isinstance(assets, list):
        return None
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    selected = {}
    for name in (ASSET, ASSET + '.sha256'):
        matches = [item for item in assets if isinstance(item, dict) and item.get('name') == name]
        if len(matches) != 1:
            return None
        asset = matches[0]
        size = asset.get('size')
        if (asset.get('browser_download_url') != base + name or type(size) is not int
                or not 0 < size <= (MAX_ARCHIVE if name == ASSET else 4096)):
            return None
        selected[name] = asset
    return {'version': version, 'prerelease': value.get('prerelease') is True,
            'published_at': value['published_at'], 'url': f'https://github.com/{REPOSITORY}/releases/tag/{tag}',
            'notes': str(value.get('body') or '')[:6000], 'archive_bytes': selected[ASSET]['size'],
            'github_digest': selected[ASSET].get('digest')}


def checked_release(version=None, *, opener=None):
    def fetch(url, limit):
        return public_bytes(url, limit, **({'opener': opener} if opener is not None else {}))
    api = f'https://api.github.com/repos/{REPOSITORY}/releases'
    if version is not None:
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            raise UpdateError('更新版本无效')
        candidates = [json.loads(fetch(api + '/tags/v' + version, 1024 * 1024))]
    else:
        candidates = json.loads(fetch(api + '?per_page=20', 4 * 1024 * 1024))
        if not isinstance(candidates, list):
            raise UpdateError('GitHub 版本列表格式无效')
    releases = [release for candidate in candidates if (release := release_summary(candidate))]
    if version is not None:
        releases = [release for release in releases if release['version'] == version]
    if not releases:
        raise UpdateError('未找到完整发布的 Debian amd64 原生安装包')
    release = max(releases, key=lambda item: item['published_at'])
    url = f'https://github.com/{REPOSITORY}/releases/download/v{release["version"]}/{ASSET}.sha256'
    checksum = fetch(url, 4096).decode('ascii').strip()
    match = re.fullmatch(r'([a-f0-9]{64})  ' + re.escape(ASSET), checksum)
    if not match:
        raise UpdateError('发行版 SHA-256 文件格式无效')
    digest = match[1]
    if release['github_digest'] not in {None, 'sha256:' + digest}:
        raise UpdateError('GitHub 安装包摘要与校验文件不一致')
    return {key: value for key, value in release.items() if key != 'github_digest'} | {'archive_sha256': digest}
