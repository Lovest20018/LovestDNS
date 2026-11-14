"""Run one verified release update outside the DNS/Web service's cgroup."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import sys
import tempfile
import time
from types import SimpleNamespace

from native import release_download
from runtime.locking import maintenance_lock
from runtime import software_update as protocol
from runtime import update_network


def installed_version(home):
    return json.loads((Path(home) / 'current/manifest.json').read_text())['version']


def change(root, value, phase, message, **extra):
    value.update(phase=phase, message=message, updated_at=time.time(), **extra)
    with protocol.protocol_lock(root):
        protocol.atomic_json(root / 'status.json', value)


def claim(root):
    with protocol.protocol_lock(root):
        request = root / 'request.json'
        try:
            job = protocol.read_json(request)
            value = protocol.read_json(root / 'status.json', {})
        except (ValueError, OSError):
            if request.exists() or request.is_symlink():
                request.replace(root / 'rejected-request.json')
            protocol.atomic_json(root / 'status.json', {'phase': 'failed', 'action': None,
                'message': '主机更新记录损坏，未执行安装；请核对后重新检查更新', 'finished_at': time.time()})
            return None
        if job is None:
            return None
        action = job.get('action')
        fields = {'job_id', 'action', 'created_at', 'previous_version'}
        if action == 'apply':
            fields |= {'version', 'archive_sha256'}
        if (set(job) != fields or action not in {'check', 'apply'}
                or not isinstance(job.get('job_id'), str)
                or not re.fullmatch(r'[a-f0-9]{32}', job['job_id'])
                or not isinstance(job.get('previous_version'), str)
                or not protocol.VERSION.fullmatch(job['previous_version'])
                or value.get('job_id') != job['job_id'] or value.get('phase') != 'queued'):
            request.replace(root / 'rejected-request.json')
            protocol.atomic_json(root / 'status.json', {'phase': 'failed', 'action': None,
                'message': '主机更新任务记录不一致，未执行安装；请重新检查更新', 'finished_at': time.time()})
            return None
        value.update(phase='checking', message='正在检查官方发行版', started_at=time.time())
        protocol.atomic_json(root / 'status.json', value)
        (root / 'request.json').unlink()
        return value


def healthy_current(p):
    installer = runpy.run_path(str(p['home'] / 'current/install.py'))
    installer['healthy'](p, timeout=10)
    return True


def space_for_install(p, package):
    payload = sum(path.stat().st_size for path in package.rglob('*') if path.is_file())
    data = sum(path.stat().st_size for name in ('data', 'state')
               for path in (p['storage'] / name).rglob('*') if path.is_file() and not path.is_symlink())
    release_budget = payload + 128 * 1024 * 1024
    backup_budget = data * 2 + 256 * 1024 * 1024
    shared = p['home'].stat().st_dev == p['storage'].stat().st_dev
    if (shutil.disk_usage(p['home']).free < release_budget + (backup_budget if shared else 0)
            or shutil.disk_usage(p['storage']).free < backup_budget + (release_budget if shared else 0)):
        raise protocol.UpdateError('磁盘可用空间不足，尚未停止 DNS 服务；请先清理旧文件')


def apply(root, p, value, instance, *, opener):
    if (not isinstance(value.get('version'), str) or not protocol.VERSION.fullmatch(value['version'])
            or not isinstance(value.get('archive_sha256'), str)
            or not protocol.DIGEST.fullmatch(value['archive_sha256'])):
        raise protocol.UpdateError('待更新版本或摘要无效')
    previous = installed_version(p['home'])
    if previous != value['previous_version'] or previous == value['version']:
        raise protocol.UpdateError('当前安装版本已变化，请重新检查更新')
    release = protocol.checked_release(value['version'], opener=opener)
    if release['archive_sha256'] != value['archive_sha256']:
        raise protocol.UpdateError('发行版摘要在确认后发生变化，请重新检查更新')
    if shutil.disk_usage(root).free < release['archive_bytes'] * 2 + 384 * 1024 * 1024:
        raise protocol.UpdateError('磁盘可用空间不足，尚未下载或停止 DNS 服务')
    change(root, value, 'downloading', '正在下载官方安装包，DNS 继续运行')
    with tempfile.TemporaryDirectory(prefix='work-', dir=root) as temporary:
        work = Path(temporary)
        archive, digest = release_download.download(value['version'], work, opener=opener)
        if digest != value['archive_sha256'] or archive.stat().st_size != release['archive_bytes']:
            raise protocol.UpdateError('下载文件与已确认的发行版不一致，未开始安装')
        change(root, value, 'verifying', '正在校验安装包与包内文件，DNS 继续运行')
        package = release_download.unpack(archive, digest, work / 'payload')
        manifest = json.loads((package / 'manifest.json').read_text())
        if manifest.get('version') != value['version'] or manifest.get('installation_receipt_v1') is not True:
            raise protocol.UpdateError('安装包版本或安装收据协议不匹配，未开始安装')
        space_for_install(p, package)
        # The shared guard waits for an existing core task and prevents a new
        # one from overlapping the installer's consistent data/state backup.
        with maintenance_lock(p['storage'] / 'state'):
            if installed_version(p['home']) != previous:
                raise protocol.UpdateError('当前安装版本已变化，请重新检查更新')
            change(root, value, 'installing', '正在备份并升级，DNS 和网页会短暂重启')
            installer = runpy.run_path(str(package / 'install.py'))
            args = SimpleNamespace(instance=instance, config=None, import_data=None, sha256=digest)
            log = root / ('install-' + value['job_id'] + '.log')
            with log.open('x', encoding='utf-8') as output, redirect_stdout(output), redirect_stderr(output):
                installer['install'](args, package)
        if installed_version(p['home']) != value['version'] or not healthy_current(p):
            raise protocol.UpdateError('安装后未能确认新版本健康状态，请检查主机')
        receipt = protocol.read_json(p['home'] / 'receipts' / ((p['home'] / 'current').resolve().name + '.json'), {})
        if receipt.get('archive_sha256') != digest:
            raise protocol.UpdateError('安装后摘要收据不匹配，请检查主机')
    change(root, value, 'succeeded', '更新完成，DNS 与网页服务已通过健康检查',
           installed_version=value['version'], finished_at=time.time(), healthy=True)


def fail(root, p, value, message):
    current, healthy = None, False
    try:
        current = installed_version(p['home'])
        healthy = healthy_current(p)
    except Exception:
        pass
    recovered = value.get('action') == 'apply' and current == value.get('previous_version') and healthy
    if recovered:
        message += '；原版本仍在正常运行'
    elif value.get('action') == 'apply' and not healthy:
        message += '；服务健康状态未通过，请在主机检查或回退'
    change(root, value, 'failed', message, installed_version=current, healthy=healthy,
           previous_version_healthy=recovered, finished_at=time.time())


def run(p, instance):
    root = p['storage'] / 'updates'
    value = claim(root)
    if value is None:
        return
    try:
        transport = update_network.opener(update_network.load(root))
        if value['action'] == 'check':
            release = protocol.checked_release(opener=transport)
            current = installed_version(p['home'])
            change(root, value, 'ready', '已是最新发布版本' if release['version'] == current else '发现可更新版本',
                   release=release, checked_at=time.time(), finished_at=time.time())
        else:
            apply(root, p, value, instance, opener=transport)
    except BaseException as exc:
        message = str(exc) if isinstance(exc, protocol.UpdateError) else (
            '检查更新失败，请稍后重试' if value['action'] == 'check' else '更新未完成，请核对结果后重试')
        fail(root, p, value, message)
        print('Native update failed: ' + type(exc).__name__, file=sys.stderr)
    finally:
        # Keep a bounded set of private installer logs; downloaded packages are
        # temporary and do not accumulate alongside rollback backups.
        logs = sorted(root.glob('install-*.log'), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in logs[5:]:
            path.unlink()


def recover(p):
    root = p['storage'] / 'updates'
    value = protocol.read_json(root / 'status.json', {})
    if value.get('phase') not in protocol.BUSY - {'queued'}:
        return
    if value.get('action') == 'apply':
        try:
            current = installed_version(p['home'])
            receipt = protocol.read_json(p['home'] / 'receipts' / ((p['home'] / 'current').resolve().name + '.json'), {})
            if current == value.get('version') and receipt.get('archive_sha256') == value.get('archive_sha256') and healthy_current(p):
                change(root, value, 'succeeded', '更新进程已结束，新版本与健康状态已重新确认',
                       installed_version=current, healthy=True, finished_at=time.time())
                return
        except Exception:
            pass
    fail(root, p, value, '主机更新进程中断，未自动重复执行')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', default='')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run through the installed systemd update service')
    os.umask(0o077)
    home = Path(__file__).resolve().parents[2]
    installer = runpy.run_path(str(home / 'install.py'))
    p = installer['paths'](args.instance)
    root = p['storage'] / 'updates'
    if not protocol.supported(root) and not (args.recover and root.is_dir() and not root.is_symlink()):
        raise SystemExit('Native update protocol is not installed')
    def interrupted(_signal, _frame):
        raise InterruptedError('Update service interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    recover(p) if args.recover else run(p, args.instance)


if __name__ == '__main__':
    main()
