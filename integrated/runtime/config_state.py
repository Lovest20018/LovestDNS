"""Evidence of successful managed actions, never a claim of rule verification."""
import hashlib
import json
import math
from pathlib import Path
import re
import time

from runtime.configuration import read_bounded

FILES = ('custom_env.ini', 'unbound_custom.conf', 'custom_mod.yaml', 'force_forward_list.txt',
         'force_dnscrypt_list.txt', 'force_recurse_list.txt', 'force_ttl_rules.txt', 'custom_cn_mark.txt', 'trackerslist.txt')


def inputs(data):
    result = {}
    for name in FILES:
        path = Path(data) / name
        if path.is_symlink():
            raise ValueError('配置文件不能是链接')
        result[name] = hashlib.sha256(read_bounded(path)).hexdigest() if path.exists() else 'missing'
    return result


def identity(core, action):
    names = ['unbound-raw'] if action == 'restart_dns_core' else ['mosdns']
    env = core.environment()
    if action == 'restart_dns_core' and env.get('CNAUTO') != 'no':
        names += ['mosdns', 'unbound-forward']
    states = core.states()
    if any(states.get(n, {}).get('statename') != 'RUNNING' for n in names):
        raise ValueError('相关 DNS 进程尚未稳定')
    result = {n: {k: states[n].get(k) for k in ('pid', 'start')} for n in names}
    if any(not isinstance(p['pid'], int) or p['pid'] <= 0 or
           not isinstance(p['start'], (int, float)) or not math.isfinite(p['start']) or p['start'] <= 0
           for p in result.values()):
        raise ValueError('相关 DNS 进程身份暂不可用')
    return result


def record(core, action, before):
    from runtime.maintenance import atomic_file
    after = inputs(core.data)
    # External host writers do not participate in our API locks. Fail closed
    # rather than associate their newly changed bytes with a completed action.
    if after != before:
        raise ValueError('应用期间文件已改变，请重新检查配置')
    document = {'action': action, 'completed_at': time.time(), 'files': before,
                'processes': identity(core, action), 'verified': False}
    core.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_file(core.state / (action + '-inputs.json'), json.dumps(document).encode())


def status(core):
    current = inputs(core.data)
    receipts = []
    for action in ('restart_dns_core', 'apply_custom_mod'):
        try:
            receipt = json.loads(read_bounded(core.state / (action + '-inputs.json')).decode())
            if (not isinstance(receipt, dict) or receipt.get('action') != action or
                    not isinstance(receipt.get('completed_at'), (int, float)) or
                    not math.isfinite(receipt['completed_at']) or receipt['completed_at'] <= 0 or
                    not isinstance(receipt.get('files'), dict) or set(receipt['files']) != set(FILES) or
                    any(not isinstance(v, str) or not re.fullmatch(r'(?:[a-f0-9]{64}|missing)', v)
                        for v in receipt['files'].values()) or not isinstance(receipt.get('processes'), dict)):
                continue
            receipt['current_processes'] = receipt.get('processes') == identity(core, action)
            receipts.append(receipt)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    rows = []
    for name, digest in current.items():
        relevant = [r for r in receipts if name in r['files'] and
                    (r['action'] == 'restart_dns_core' or name == 'custom_mod.yaml')]
        last = max(relevant, key=lambda r: r['completed_at'], default=None)
        evidence = last if last and last['current_processes'] else None
        rows.append({'filename': name, 'revision': digest,
                     'state': 'unknown' if evidence is None else 'recorded' if evidence['files'][name] == digest else 'changed',
                     'recorded_revision': evidence['files'][name] if evidence else None,
                     'recorded_at': last['completed_at'] if last else None,
                     'action': last['action'] if last else None, 'verified': False})
    return {'files': rows, 'sampled_at': time.time(), 'verified': False,
            'scope': '仅比较当前文件与成功维护任务记录的输入；进程改变后需重新确认。监听器自动加载不作为确认记录；域名规则效果需查询验证。'}
