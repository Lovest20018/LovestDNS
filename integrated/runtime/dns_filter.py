"""Manage private, immutable DNS filter revisions and verify their activation."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import struct
import threading
import time
import uuid

from runtime.filter_download import destination, download
from runtime.filter_rules import MAX_LIST_BYTES, MAX_POLICY_BYTES, compile_policy, digest, encode_policy, encoded, normalized_domain, parse
from runtime.locking import maintenance_lock
from runtime.maintenance import atomic_file
from runtime.query_wire import dns_message

REVISION = re.compile('[a-f0-9]{64}')
ID = re.compile('[a-f0-9]{32}')
INTERVALS = (0, 21600, 86400, 604800)
MAX_SOURCES = 12
MAX_LOCAL_BYTES = 16 * 1024
MAX_ALLOW = 2048


def core_call(run, op, timeout=.35, **data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(Path(run) / 'dns-filter.sock'))
        sock.sendall(encoded({'op': op, **data}) + b'\n')
        with sock.makefile('rb') as stream:
            raw = stream.readline(8193)
    if not raw.endswith(b'\n') or len(raw) > 8192:
        raise OSError('filter control response is incomplete')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise OSError('filter control rejected the request')
    return value


def dns_probe(endpoint, name, expected):
    host, port = endpoint['address'].rsplit(':', 1)
    ip = ipaddress.ip_address(host.strip('[]'))
    host = str(ip) if not ip.is_unspecified else ('::1' if ip.version == 6 else '127.0.0.1')
    tcp = endpoint['transport'] == 'tcp'
    with socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                       socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(.8)
        sock.connect((host, int(port)))
        request_id = int.from_bytes(os.urandom(2), 'big')
        qname = b''.join(bytes([len(label)]) + label.encode('ascii') for label in name.split('.'))
        query = struct.pack('!6H', request_id, 256, 1, 0, 0, 0) + qname + b'\0\0\1\0\1'
        sock.sendall(struct.pack('!H', len(query)) + query if tcp else query)
        if tcp:
            with sock.makefile('rb') as stream:
                prefix = stream.read(2)
                wire = stream.read(int.from_bytes(prefix, 'big'))
        else:
            wire = sock.recv(4096)
    value = dns_message(wire)
    if (not value['response'] or value['dns_id'] != request_id or value['domain'] != name + '.'
            or value['qtype'] != 1 or value['rcode'] != expected):
        raise ValueError('过滤探测应答与候选版本不一致')


def activate_core(run, revision, *, verify=True):
    loaded = core_call(run, 'load', timeout=5, revision=revision)
    if loaded.get('revision') != revision:
        raise ValueError('解析核心未确认候选过滤版本')
    if not verify:
        return
    endpoints = loaded.get('endpoints') or []
    probes = loaded.get('probes') or []
    if {ep['transport'] for ep in endpoints} != {'udp', 'tcp'} or len(endpoints) > 4 or len(probes) != 2:
        raise ValueError('需要确认 MosDNS 的 UDP 和 TCP 监听')
    for endpoint in endpoints:
        dns_probe(endpoint, probes[0], 5 if loaded['enabled'] else 0)
        dns_probe(endpoint, probes[1], 0)
    active = core_call(run, 'status')
    if active['epoch'] != loaded['epoch'] or active['revision'] != revision:
        raise ValueError('验证期间 MosDNS 已重启或版本已变化')


def read(path, limit):
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('过滤配置文件超出上限')
    return value


def durable(path, value):
    atomic_file(path, value)
    if os.name == 'posix':
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class FilterStore:
    def __init__(self, state, run):
        self.root = Path(state) / 'dns-filter'
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run = Path(run)

    def revision(self, name='active'):
        try:
            value = read(self.root / name, 64).decode('ascii')
        except FileNotFoundError:
            return ''
        if value and not REVISION.fullmatch(value):
            raise ValueError('过滤配置版本无效')
        return value

    def model(self, revision):
        if not revision:
            return {'enabled': False, 'local_rules': '', 'sources': [], 'allow': {}, 'rules': 0}
        if not REVISION.fullmatch(revision):
            raise ValueError('过滤配置版本无效')
        return json.loads(read(self.root / (revision + '.model'), 256 * 1024))

    def list_text(self, key):
        if not REVISION.fullmatch(key):
            raise ValueError('订阅摘要无效')
        data = read(self.root / (key + '.list'), MAX_LIST_BYTES)
        if digest(data) != key:
            raise ValueError('订阅摘要不匹配')
        return data.decode()

    def save_list(self, text):
        data = text.encode()
        key = digest(data)
        path = self.root / (key + '.list')
        if not path.exists():
            durable(path, data)
        return key

    def apply(self, old, model, *, allow_only=False):
        cutoff = int(time.time())
        model['allow'] = {name: end for name, end in model['allow'].items() if end is None or end > cutoff}
        if allow_only and old:
            previous = read(self.root / (old + '.policy'), MAX_POLICY_BYTES)
            if digest(previous) != old:
                raise ValueError('活动策略摘要不匹配')
            policy = encode_policy(json.loads(previous), model, cutoff)
            total = model['rules']
        else:
            policy, total = compile_policy(model, self.list_text, cutoff)
        new = digest(policy)
        meta = {**model, 'rules': total}
        with maintenance_lock(self.root.parent, timeout=5):
            if self.revision() != old:
                raise ValueError('过滤配置已变化，请刷新后重试')
            durable(self.root / (new + '.policy'), policy)
            durable(self.root / (new + '.model'), encoded(meta))
            try:
                activate_core(self.run, new)
                if new != old:
                    durable(self.root / 'previous', old.encode())
                durable(self.root / 'active', new.encode())
            except (OSError, ValueError, KeyError) as exc:
                try:
                    committed = self.revision()
                    activate_core(self.run, committed, verify=False)
                except (OSError, ValueError, KeyError):
                    raise ValueError('过滤应用失败，核心尚未确认磁盘活动版本，后台会继续核对') from exc
                if committed == new:
                    raise ValueError('核心已载入新版本，磁盘同步尚未确认') from exc
                raise ValueError('过滤候选应用失败，已恢复上一活动版本') from exc
        return new

    def prune(self):
        revisions = {self.revision(), self.revision('previous')} - {''}
        contents = {item['content'] for revision in revisions for item in self.model(revision)['sources']}
        for path in self.root.iterdir():
            if not REVISION.fullmatch(path.stem):
                continue
            if path.suffix == '.list' and path.stem not in contents:
                path.unlink(missing_ok=True)
            if path.suffix in ('.policy', '.model') and path.stem not in revisions:
                path.unlink(missing_ok=True)


def validate_action(value):
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('过滤操作无效')
    action = value['action']
    fields = {
        'configure': {'enabled', 'local_rules'},
        'preview': {'url', 'name', 'interval'},
        'apply_preview': {'preview', 'partial'},
        'refresh': {'id'},
        'subscription': {'id', 'enabled', 'interval', 'remove'},
        'allow': {'domain', 'mode'},
    }
    if action == 'preview':
        value = {'type': 'block', **value}
        fields['preview'].add('type')
    if (action not in fields or set(value) != {'action', 'revision', *fields[action]}
            or not isinstance(value['revision'], str)
            or value['revision'] and not REVISION.fullmatch(value['revision'])):
        raise ValueError('过滤操作字段无效，请刷新配置')
    if action == 'configure':
        if (type(value['enabled']) is not bool or not isinstance(value['local_rules'], str)
                or len(value['local_rules'].encode('utf-8')) > MAX_LOCAL_BYTES):
            raise ValueError('本地规则须小于 16 KiB')
    if action == 'preview':
        if value['type'] not in ('block', 'allow'):
            raise ValueError('订阅类型无效')
        destination(value['url'])
        name = value['name']
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or any(ord(c) < 32 for c in name):
            raise ValueError('订阅名称须为 1 至 64 字符')
    if 'interval' in value and (type(value['interval']) is not int or value['interval'] not in INTERVALS):
        raise ValueError('刷新周期无效')
    for key in ('id', 'preview'):
        if key in value and (not isinstance(value[key], str) or not ID.fullmatch(value[key])):
            raise ValueError('订阅或预览编号无效')
    if any(type(value[key]) is not bool for key in ('enabled', 'remove', 'partial') if key in value):
        raise ValueError('过滤操作开关无效')
    if action == 'allow':
        value = {**value, 'domain': normalized_domain(value['domain'])}
        if value['mode'] not in ('temporary', 'permanent', 'remove'):
            raise ValueError('放行方式无效')
    return value


class FilterManager:
    def __init__(self, state, run, downloader=download):
        self.store = FilterStore(state, run)
        self.download = downloader
        self.lock = threading.Lock()
        self.jobs = {}
        self.work = None
        self.draft = None
        self.retries = {}

    def start(self):
        threading.Thread(target=self.run, name='dns-filter-manager', daemon=True).start()

    def supported(self):
        if (self.store.run / 'dns-filter.sock').exists():
            return True
        try:
            info = Path(os.environ.get('SUITE_BUILD_INFO', '/opt/suite/build-info.json'))
            return isinstance(json.loads(read(info, 128 * 1024)).get('mosdns_filter'), dict)
        except (OSError, ValueError, AttributeError):
            return False

    def core_status(self):
        try:
            env = json.loads(read(self.store.run / 'core-env.json', 128 * 1024))
            if env.get('CNAUTO') == 'no':
                return 'recursive', None
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            return 'mosdns', core_call(self.store.run, 'status')
        except (OSError, ValueError):
            return 'unavailable', None

    def status(self):
        revision = self.store.revision()
        model = self.store.model(revision)
        mode, runtime = self.core_status()
        with self.lock:
            draft = self.draft
            jobs = list(self.jobs.values())
        now = time.time()
        safe = {**model,
                'allow': {name: end for name, end in model['allow'].items() if end is None or end > now},
                'sources': [{'type': 'block', **{k: v for k, v in item.items() if k not in ('url', 'content', 'accepted')}}
                            for item in model['sources']]}
        return {**safe, 'revision': revision, 'mode': mode, 'available': runtime is not None,
                'live_revision': runtime['revision'] if runtime else None, 'live_enabled': runtime['enabled'] if runtime else False,
                'draft': draft['public'] if draft else None, 'jobs': jobs[::-1]}

    def check(self, domain):
        domain = normalized_domain(domain)
        mode, runtime = self.core_status()
        if runtime is None:
            raise RuntimeError('当前入口未提供 DNS 过滤')
        try:
            value = core_call(self.store.run, 'check', domain=domain)
        except (OSError, ValueError) as exc:
            raise RuntimeError('过滤核心暂不可用') from exc
        if value['epoch'] != runtime['epoch']:
            raise RuntimeError('MosDNS 正在重启，请重试')
        return {'domain': domain, **value['decision']}

    def submit(self, data):
        data = validate_action(data)
        if data['revision'] != self.store.revision():
            raise ValueError('过滤配置已变化，请刷新后重试')
        with self.lock:
            if self.work is not None:
                raise ValueError('已有过滤任务正在执行')
            job = uuid.uuid4().hex
            self.jobs[job] = {'id': job, 'action': data['action'], 'phase': 'pending', 'message': ''}
            for key in list(self.jobs)[:-8]:
                del self.jobs[key]
            self.work = job, data
            return {'id': job}

    def run(self):
        while True:
            try:
                self.step()
            except (OSError, ValueError, RuntimeError, KeyError):
                pass
            time.sleep(.35)

    def step(self):
        with self.lock:
            work = self.work
        if work:
            job, data = work
            try:
                message = self.execute(data)
                phase = 'succeeded'
            except (ValueError, RuntimeError) as exc:
                phase, message = 'failed', str(exc)
            except (OSError, KeyError):
                phase, message = 'failed', '过滤任务失败，请检查运行状态'
            with self.lock:
                self.jobs[job] = {**self.jobs[job], 'phase': phase, 'message': message}
                self.work = None
            try:
                self.store.prune()
            except (OSError, ValueError):
                pass
            return
        revision = self.store.revision()
        _, runtime = self.core_status()
        if runtime is None:
            return
        if runtime['revision'] != revision:
            # The durable pointer wins over a candidate left in RAM by an
            # interrupted manager. Never replay an uncommitted job.
            with maintenance_lock(self.store.root.parent, timeout=.05):
                activate_core(self.store.run, revision, verify=False)
            self.store.prune()
            return
        now = time.time()
        model = self.store.model(revision)
        sources = {item['id'] for item in model['sources']}
        self.retries = {key: timestamp for key, timestamp in self.retries.items() if key in sources and timestamp > now}
        for item in model['sources']:
            key, interval = item['id'], item['interval']
            if item['enabled'] and interval and now >= max(item['updated'] + interval, self.retries.get(key, 0)):
                try:
                    self.submit({'action': 'refresh', 'revision': revision, 'id': key})
                except ValueError:
                    break
                self.retries[key] = now + 3600
                break

    def execute(self, data):
        old, action = data['revision'], data['action']
        if old != self.store.revision():
            raise ValueError('过滤配置已变化，请刷新后重试')
        model = self.store.model(old)
        if action == 'preview':
            if len(model['sources']) >= MAX_SOURCES:
                raise ValueError('已达到订阅数量上限')
            text = self.download(data['url'])
            return self.save_draft(old, data, text, existing=None)
        _, runtime = self.core_status()
        if runtime is None:
            raise ValueError('当前入口未提供 DNS 过滤')
        if action == 'configure':
            _, parsed = parse(data['local_rules'])
            if any(parsed[k] for k in ('invalid', 'unsupported', 'protected')):
                first = parsed['issues'][0]
                raise ValueError(f"本地规则第 {first['line']} 行无法应用（{first['reason']}）")
            model.update(enabled=data['enabled'], local_rules=data['local_rules'])
        elif action == 'apply_preview':
            draft = self.draft
            if draft is None or draft['public']['id'] != data['preview'] or draft['revision'] != old:
                raise ValueError('预览已过期，请重新下载')
            parsed = draft['public']['parse']
            if parsed['protected'] or not parsed['valid']:
                raise ValueError('候选列表含无法执行的例外、预处理指令或无有效规则')
            if (parsed['invalid'] or parsed['unsupported']) and not data['partial']:
                raise ValueError('请先检查不能导入的规则，再确认部分导入')
            item = {**draft['item'], 'content': self.store.save_list(draft['text']), 'updated': int(time.time()),
                    'accepted': parsed['types'], 'parse': omit_issues(parsed)}
            if draft['new']:
                model['sources'].append(item)
            else:
                model['sources'] = [item if row['id'] == item['id'] else row for row in model['sources']]
        elif action == 'allow':
            name, mode = data['domain'], data['mode']
            if mode == 'remove':
                model['allow'].pop(name, None)
            else:
                model['allow'] = {n: end for n, end in model['allow'].items() if end is None or end > time.time()}
                if name not in model['allow'] and len(model['allow']) >= MAX_ALLOW:
                    raise ValueError('本地放行已达到数量上限')
                if mode == 'permanent' or name not in model['allow'] or model['allow'][name] is not None:
                    model['allow'][name] = int(time.time()) + 600 if mode == 'temporary' else None
        else:
            item = next((row for row in model['sources'] if row['id'] == data['id']), None)
            if item is None:
                raise ValueError('订阅已不存在')
            if action == 'refresh':
                text = self.download(item['url'])
                _, parsed = parse(text, source_type=item.get('type', 'block'))
                if (not parsed['valid'] or parsed['protected'] or set(parsed['types']) - set(item['accepted'])
                        or parsed['invalid'] > item['parse']['invalid']):
                    self.save_draft(old, item, text, existing=item, parsed=parsed)
                    raise ValueError('订阅更新需要复核，已保留上一版')
                item.update(content=self.store.save_list(text), updated=int(time.time()), parse=omit_issues(parsed))
            elif data['remove']:
                model['sources'].remove(item)
            else:
                item.update(enabled=data['enabled'], interval=data['interval'])
        revision = self.store.apply(old, model, allow_only=action == 'allow')
        with self.lock:
            self.draft = None
        return '已应用过滤版本 ' + revision[:12]

    def save_draft(self, revision, data, text, *, existing, parsed=None):
        if parsed is None:
            _, parsed = parse(text, source_type=data.get('type', 'block'))
        value = destination(data['url'])
        item = copy.copy(existing) if existing else {
            'id': uuid.uuid4().hex, 'url': data['url'], 'name': data['name'],
            'host': value.hostname, 'interval': data['interval'], 'enabled': True, 'type': data['type']}
        public = {'id': uuid.uuid4().hex, 'source_id': item['id'], 'name': item['name'], 'host': item['host'],
                  'type': item.get('type', 'block'), 'parse': parsed}
        with self.lock:
            self.draft = {'public': public, 'revision': revision, 'item': item, 'new': existing is None, 'text': text}
        return '订阅预览已生成'


def omit_issues(parsed):
    return {k: v for k, v in parsed.items() if k not in ('issues', 'types')}
