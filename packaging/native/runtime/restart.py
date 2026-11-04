"""Restart only the fixed DNS components; keep Web, collector and task worker alive."""
import json
import time
import xmlrpc.client

from native.environment import validate
from runtime.component import read_overrides
from runtime.locking import maintenance_lock
from runtime.maintenance import ActionFailed, atomic_file
from runtime.observer import UnixTransport, UnixHTTPConnection

ORDER = ('redis', 'dnscrypt', 'dnscrypt-socks', 'unbound-forward', 'unbound-raw', 'mosdns', 'cron', 'watcher', 'updater')
ACTIVE = {'RUNNING', 'STARTING', 'BACKOFF'}


class ControlConnection(UnixHTTPConnection):
    def connect(self):
        super().connect()
        self.sock.settimeout(150)


class ControlTransport(UnixTransport):
    def make_connection(self, host):
        return ControlConnection(host)


def control(action, name):
    if action not in {'start', 'stop'} or name not in {*ORDER, 'bootstrap'}:
        raise ValueError('Unsupported DNS component')
    with xmlrpc.client.ServerProxy('http://localhost/RPC2', transport=ControlTransport()) as rpc:
        if action == 'stop':
            rpc.supervisor.stopProcess(name, True)
        else:
            rpc.supervisor.startProcess(name, True)


def stop(core):
    for name in ('bootstrap', *reversed(ORDER)):
        if core.states().get(name, {}).get('statename') in ACTIVE:
            control('stop', name)


def restart(core):
    with maintenance_lock(core.state):
        core.environment()
        validate(read_overrides(core.data / 'custom_env.ini'))
        from runtime.config_state import inputs, record
        recorded_inputs = inputs(core.data)
        before = core.states()
        if before.get('bootstrap', {}).get('statename') in ACTIVE:
            raise ValueError('核心正在初始化，请稍后重试')
        if before.get('redis', {}).get('statename') != 'RUNNING':
            raise ValueError('Redis 未运行，请先在主机检查服务')
        # Snapshot every regular runtime file; sockets and directories are not copied.
        paths = [p for p in core.temporary.iterdir() if p.is_file() and not p.is_symlink()]
        paths += [core.run / name for name in ('core-env.json', 'mosdns-env.json')]
        resolver = core.data / 'dnscrypt-resolvers'
        paths += [resolver / name for name in ('dnscrypt.toml', 'dnscrypt_socks.toml')]
        files, total = {}, 0
        for path in paths:
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('运行配置过大或包含链接，请通过主机维护命令重启')
            total += path.stat().st_size
            if total > 128 * 1024 * 1024:
                raise ValueError('运行配置备份超过限制，请通过主机维护命令重启')
            files[path] = path.read_bytes()
        atomic_file(core.state / 'last-runtime-before-restart.json',
                    json.dumps({str(p): data.hex() for p, data in files.items()}).encode())
        # A verified BGSAVE happens before any service is stopped. Redis is stopped
        # last and started first, retaining both databases across mode changes.
        core.save_snapshot_unlocked()
        changed = False
        try:
            changed = True
            stop(core)
            for name in ('ready', 'mosdns-env.json'):
                (core.run / name).unlink(missing_ok=True)
            control('start', 'bootstrap')
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                state = core.states().get('bootstrap', {})
                if state.get('statename') in {'EXITED', 'FATAL', 'STOPPED'}:
                    if state.get('statename') != 'EXITED' or state.get('exitstatus') != 0:
                        raise ValueError('新配置初始化失败')
                    if not (core.run / 'ready').exists():
                        raise ValueError('新配置未完成初始化')
                    env = core.environment()
                    required = ['redis', 'unbound-raw', 'watcher']
                    if env.get('CNAUTO') != 'no':
                        required += ['mosdns', 'unbound-forward', 'dnscrypt']
                    for name in required:
                        core.wait_running(name)
                    probe = core.probe()
                    record(core, "restart_dns_core", recorded_inputs)
                    return {'state': 'restarted_unverified', 'verified': False, 'probe': probe,
                            'message': 'DNS 核心已按保存配置重启，服务已响应；请查询目标域名验证规则。'}
                time.sleep(0.5)
            raise ValueError('新配置初始化超时')
        except Exception as exc:
            rollback = {'attempted': changed, 'runtime_restored': False, 'service_running': False}
            try:
                stop(core)
                for path, data in files.items():
                    atomic_file(path, data)
                if core.run / 'mosdns-env.json' not in files:
                    (core.run / 'mosdns-env.json').unlink(missing_ok=True)
                rollback['runtime_restored'] = True
                for name in ORDER:
                    if before.get(name, {}).get('statename') == 'RUNNING':
                        control('start', name)
                        core.wait_running(name)
                core.probe()
                (core.run / 'ready').touch()
                rollback['service_running'] = True
            except Exception:
                rollback['error'] = '旧运行配置恢复失败，请检查主机服务及 bootstrap 日志'
            raise ActionFailed('DNS 重启失败；保存的配置仍保留，请检查回退结果后修正配置', {'rollback': rollback}) from exc
