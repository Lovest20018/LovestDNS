"""Optional, browser-admin-only read adapter. No configurable proxy routes or background IO."""
from contextlib import contextmanager
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import time
import uuid
from urllib.parse import urlsplit, urlencode

from flask import jsonify, request
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, ServiceUnavailable, BadGateway
from browser_auth import current_session, csrf_valid
from runtime.locking import maintenance_lock, MaintenanceBusy
from runtime.admission import slots, CapacityBusy

LIMIT = 512 * 1024
FEATURES = ('connections', 'connection_history', 'persistent_history', 'mihomo_connection_snapshots', 'fakeip_lookup')

def target(value):
    if not isinstance(value, str) or len(value) > 256 or any(c.isspace() for c in value):
        raise BadRequest('请输入固定 API 地址')
    try:
        u = urlsplit(value)
        ip = ipaddress.ip_address(u.hostname or '')
        port = u.port or (443 if u.scheme == 'https' else 80)
        if u.scheme not in ('http', 'https') or u.username or u.password or u.query or u.fragment or u.path.rstrip('/') != '/api/v1':
            raise ValueError()
        if ip.is_unspecified or ip.is_multicast or ip.is_link_local or (ip.is_reserved and not ip.is_loopback) or '%' in (u.hostname or '') or not 1 <= port <= 65535:
            raise ValueError()
        if u.scheme == 'http' and not (ip.is_private or ip.is_loopback):
            raise ValueError()
    except (ValueError, TypeError):
        raise BadRequest('API 地址需为固定 IP 的 http(s)://IP:端口/api/v1；HTTP 仅允许内网或回环地址') from None
    host = f'[{ip}]' if ip.version == 6 else str(ip)
    return f'{u.scheme}://{host}:{port}/api/v1'

def scalar(value):
    return value if value is None or type(value) in (bool, int, float) or isinstance(value, str) and len(value) <= 2048 else None

def project(value, keys):
    return {k: scalar(value[k]) for k in keys if k in value} if isinstance(value, dict) else {}

def wire(config, endpoint, params=None):
    # Literal IP only: no resolver, environment proxy, redirects, or arbitrary path.
    u = urlsplit(target(config['url']))
    cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
    kwargs = {'timeout': 6}
    if u.scheme == 'https': kwargs['context'] = ssl.create_default_context()
    conn = cls(u.hostname, u.port, **kwargs)
    deadline = time.monotonic() + 6
    try:
        conn.request('GET', u.path + '/' + endpoint + ('?' + urlencode(params) if params else ''), headers={'Authorization': 'Bearer ' + config['token'], 'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if response.status == 409:
            raw = response.read(LIMIT + 1)
            if len(raw) <= LIMIT:
                try:
                    code = json.loads(raw).get('error', {}).get('code')
                except (ValueError, AttributeError): code = None
                if code in ('CURSOR_EXPIRED', 'CORE_CHANGED'):
                    raise Conflict(code + '：网关观测窗口已改变，请从首页重新读取')
            raise Conflict('网关观测状态已改变，请从首页重新读取')
        if response.status != 200:
            raise BadGateway(f'网关响应 HTTP {response.status}，请检查服务及只读凭据')
        if response.getheader('Content-Encoding', 'identity') != 'identity': raise ValueError()
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > LIMIT): raise ValueError()
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError()
            if conn.sock: conn.sock.settimeout(remaining)
            chunk = response.read1(min(16384, LIMIT + 1 - len(payload)))
            if not chunk: break
            payload.extend(chunk)
            if len(payload) > LIMIT: raise ValueError()
        body = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(body, dict) or body.get('api_version') != '1' or not isinstance(body.get('instance_id'), str) or not isinstance(body.get('data'), dict): raise ValueError()
        expected = config.get('instance_id')
        if expected and body['instance_id'] != expected: raise Conflict('网关身份已改变，请重新提供 Token 并保存连接')
        for key in ('sampled_at', 'fetched_at'):
            if body.get(key) is not None and (type(body[key]) not in (int, float) or not math.isfinite(body[key]) or body[key] < 0): raise ValueError()
        return body
    except (OSError, socket.timeout, TimeoutError, http.client.HTTPException):
        raise BadGateway('网关连接失败或超时，请检查固定地址、服务与证书') from None
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise BadGateway('网关响应格式或大小无效') from None
    finally:
        conn.close()

class Store:
    def __init__(self, root): self.root = Path(root); self.path = self.root / 'gateway-integration.json'
    def read(self):
        if self.path.is_symlink(): raise ServiceUnavailable('网关联动配置不可用')
        if not self.path.exists(): return {'revision': 'missing', 'enabled': False}
        try:
            with self.path.open('rb') as stream: raw = stream.read(8193)
            if len(raw) > 8192: raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError()
            return value
        except (OSError, ValueError): raise ServiceUnavailable('网关联动配置不可用') from None
    def public(self, value=None):
        value = self.read() if value is None else value
        return {**project(value, ('revision', 'enabled', 'url', 'instance_id', 'name', 'version')), 'has_token': bool(value.get('token'))}
    def candidate(self, body, old):
        if not isinstance(body, dict) or set(body) - {'url', 'token', 'expected_revision'}: raise BadRequest('联动配置参数无效')
        url = target(body.get('url'))
        token = body.get('token')
        if token == '' or token is None:
            if url != old.get('url') or not old.get('token'): raise BadRequest('新目标必须重新提供只读 Token')
            token = old['token']
        if not isinstance(token, str) or not re.fullmatch(r'[!-~]{32,256}', token): raise BadRequest('只读 Token 需为 32–256 个非空白 ASCII 字符')
        return {'url': url, 'token': token, **({'instance_id': old['instance_id']} if url == old.get('url') and not body.get('token') and old.get('instance_id') else {})}
    def save(self, value, expected):
        # Random revision carries no secret-derived digest. State is included by existing native/container backups.
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with maintenance_lock(self.root / 'gateway-config-lock'):
            if self.read().get('revision') != expected: raise Conflict('联动配置已改变，请重新读取')
            value = {**value, 'revision': uuid.uuid4().hex}
            temp = self.root / ('.gateway-' + uuid.uuid4().hex)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
        return self.public(value)

def summary(config):
    identity = wire(config, 'instance')
    if identity['data'].get('kind') != 'paopaogateway': raise BadGateway('目标不是兼容的 LovestGateway 实例')
    selected = {**config, 'instance_id': identity['instance_id']}
    caps = wire(selected, 'capabilities'); runtime = wire(selected, 'runtime')
    if caps['data'].get('access_role') != 'observer' or caps['data'].get('instance_id') != identity['instance_id']: raise BadGateway('需要兼容的网关只读 observer Token')
    feature = caps['data'].get('features', {})
    features = {k: feature.get(k) is True for k in FEATURES} if isinstance(feature, dict) else {}
    data = runtime['data']
    return {'instance': {**project(identity['data'], ('name', 'version', 'kind')), 'id': identity['instance_id']}, 'features': features,
            'mode': scalar(caps['data'].get('mode')), 'runtime': project(data, ('status', 'core_responding', 'outbound_reachable', 'forwarding_verified', 'evidence_schema')),
            'sampled_at': runtime.get('sampled_at'), 'fetched_at': runtime.get('fetched_at'), 'freshness': project(data.get('freshness'), ('stale', 'age_seconds', 'max_age_seconds'))}

def connection_row(row, source, history=None):
    if not isinstance(row, dict): raise BadGateway('网关记录格式无效')
    metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    decision = project(row.get('decision'), ('status', 'source', 'rule_id', 'rule_kind', 'reason', 'outbound_id', 'connect_result', 'application_result'))
    chains = row.get('chains', [])
    return {'id': scalar(row.get('id')), 'source': source, 'domain': scalar(row.get('domain') or metadata.get('host')),
            'client': scalar(row.get('source_address') or metadata.get('sourceIP')), 'destination': scalar(row.get('original_destination') or metadata.get('destinationIP')),
            'actual_target': scalar(row.get('actual_target_address')), 'upstream_peer': scalar(row.get('upstream_peer')),
            'state': scalar(row.get('state')), 'started_at': scalar(row.get('started_at') or row.get('start')), 'decision': decision,
            'rule': scalar(row.get('rule')), 'rule_payload': scalar(row.get('rulePayload')),
            'chains': [x[:256] for x in chains[:20] if isinstance(x, str)] if isinstance(chains, list) else [],
            'first_seen_at': scalar((history or {}).get('first_seen_at')), 'last_seen_at': scalar((history or {}).get('last_seen_at'))}

def filters(args):
    if any(len(args.getlist(k)) != 1 for k in args): raise BadRequest('筛选参数重复')
    if set(args) - {'view', 'domain', 'client', 'cursor', 'before'}: raise BadRequest('筛选参数无效')
    value = {}
    if args.get('domain'):
        domain = args['domain'].lower().rstrip('.')
        if not re.fullmatch(r'[a-z0-9._-]{1,253}', domain): raise BadRequest('域名格式无效')
        value['domain'] = domain
    if args.get('client'):
        try: value['client'] = str(ipaddress.ip_address(args['client']))
        except ValueError: raise BadRequest('客户端 IP 格式无效') from None
    for name in ('cursor', 'before'):
        if args.get(name):
            if len(args[name]) > 512 or not re.fullmatch(r'[A-Za-z0-9:_-]+', args[name]): raise BadRequest('分页参数无效')
            if name == 'before' and (not args[name].isdigit() or not 0 < int(args[name]) < 2**63): raise BadRequest('分页参数无效')
            value[name] = args[name]
    return value

def register_routes(api, server, meta):
    def store(): return Store(server.STATE_DIR)
    @api.errorhandler(MaintenanceBusy)
    def gateway_busy(_exc): return jsonify({'error': {'code': 'UNAVAILABLE', 'message': '网关配置繁忙，请稍后重新读取'}, 'meta': meta()}), 503
    @api.before_request
    def browser_only():
        if request.path.startswith('/api/v1/gateway/'):
            if request.content_length and request.content_length > 8192: raise BadRequest('联动请求过大')
            session = current_session(server.STATE_DIR)
            if request.headers.get('Authorization') or not session or session['initial']: raise Forbidden('网关联动仅供已登录的管理员使用')
            if request.method != 'GET' and not csrf_valid(session): raise Forbidden('登录校验已失效，请刷新页面')
    def response(value): return jsonify({'data': value, 'meta': meta()})
    @contextmanager
    def capacity():
        try:
            with slots(server.RUN_DIR / 'gateway-slots', 2): yield
        except CapacityBusy: raise ServiceUnavailable('网关查询繁忙，请稍后重试') from None
    def configured():
        value = store().read()
        if not value.get('enabled') or not value.get('token'): raise Conflict('尚未启用网关联动，请前往设置连接')
        return value
    @api.get('/gateway/config')
    def gateway_config(): return response(store().public())
    @api.post('/gateway/test')
    def gateway_test():
        candidate = store().candidate(request.get_json(silent=True), store().read())
        with capacity(): return response(summary(candidate))
    @api.put('/gateway/config')
    def gateway_save():
        body = request.get_json(silent=True); old = store().read(); candidate = store().candidate(body, old)
        if body.get('expected_revision') != old.get('revision'): raise Conflict('联动配置已改变，请重新读取')
        with capacity(): checked = summary(candidate)
        return response(store().save({**candidate, 'enabled': True, 'instance_id': checked['instance']['id'], 'name': checked['instance'].get('name'), 'version': checked['instance'].get('version')}, body['expected_revision']))
    @api.delete('/gateway/config')
    def gateway_disconnect():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'expected_revision'}: raise BadRequest('缺少配置版本')
        return response(store().save({'enabled': False}, body['expected_revision']))
    @api.get('/gateway/summary')
    def gateway_summary():
        config = configured()
        with capacity(): return response(summary(config))
    @api.get('/gateway/connections')
    def gateway_connections():
        config = configured(); query = filters(request.args); view = request.args.get('view', 'recent')
        if view not in ('recent', 'history', 'mihomo'): raise BadRequest('记录视图无效')
        if (view != 'recent' and 'cursor' in query) or (view != 'history' and 'before' in query): raise BadRequest('分页参数不适用于该视图')
        with capacity():
            caps = wire(config, 'capabilities')['data']; features = caps.get('features', {})
            feature = {'recent': 'connection_history', 'history': 'persistent_history', 'mihomo': 'mihomo_connection_snapshots'}[view]
            if not isinstance(features, dict) or features.get(feature) is not True: raise Conflict('网关当前未提供此观测能力')
            params = {'limit': 50, **query}
            if view == 'recent': params['view'] = 'recent'
            if view == 'mihomo': params = {'limit': 100}
            envelope = wire(config, {'recent': 'connections', 'history': 'history', 'mihomo': 'mihomo/connections'}[view], params)
        data = envelope['data']; items = data.get('items')
        if not isinstance(items, list) or len(items) > (100 if view == 'mihomo' else 50): raise BadGateway('网关记录格式无效')
        rows = []
        for item in items:
            if not isinstance(item, dict): raise BadGateway('网关记录格式无效')
            source = item.get('source') if view == 'history' else 'mihomo_connection_snapshot' if view == 'mihomo' else 'sniffbox_lifecycle'
            row = connection_row(item.get('record') if view == 'history' else item, source if source in ('mihomo_snapshot', 'mihomo_connection_snapshot', 'sniffbox_lifecycle') else 'unknown', item if view == 'history' else None)
            if view != 'mihomo' or (not query.get('domain') or row['domain'] == query['domain']) and (not query.get('client') or row['client'] == query['client']): rows.append(row)
        return response({'items': rows, 'view': view, 'sampled_at': envelope.get('sampled_at'), 'fetched_at': envelope.get('fetched_at'), 'freshness': project(data.get('freshness'), ('stale', 'age_seconds', 'max_age_seconds')), 'complete_history': False,
                         'scope': scalar(data.get('scope')) or ('persistent_observations' if view == 'history' else 'mihomo_connection_snapshot' if view == 'mihomo' else 'tcp_after_sniff'),
                         'filter_scope': '前 100 条快照内本地筛选' if view == 'mihomo' else '网关精确筛选', 'next_cursor': scalar(data.get('next_cursor')), 'next_before': scalar(data.get('next_before')), 'gap': scalar(data.get('gap'))})
    @api.get('/gateway/fakeip')
    def gateway_fakeip():
        config = configured()
        if set(request.args) not in ({'domain'}, {'ip'}) or any(len(request.args.getlist(k)) != 1 for k in request.args): raise BadRequest('域名或 IP 二选一')
        name = next(iter(request.args)); value = request.args[name]
        if name == 'domain':
            value = filters(request.args).get('domain')
            if not value: raise BadRequest('请输入域名')
        else:
            try: value = str(ipaddress.ip_address(value))
            except ValueError: raise BadRequest('IP 格式无效') from None
        with capacity():
            features = wire(config, 'capabilities')['data'].get('features', {})
            if not isinstance(features, dict) or features.get('fakeip_lookup') is not True: raise Conflict('网关当前未提供 FakeIP 查询')
            envelope = wire(config, 'fakeip/lookup', {name: value})
        return response({**project(envelope['data'], ('status', 'source', 'domain', 'fakeip', 'cidr', 'dns_ttl_seconds', 'valid_until', 'validity', 'historical_ownership_verified')), 'sampled_at': envelope.get('sampled_at'), 'fetched_at': envelope.get('fetched_at')})
