"""Private software-update proxy settings and HTTPS tunnels (stdlib only)."""
import base64
import http.client
import ipaddress
import socket
import struct
import urllib.request

from runtime import software_update as protocol

DEFAULT = {'mode': 'direct', 'host': '', 'port': None, 'username': '', 'password': '', 'channel': 'stable'}


def validate(value):
    if isinstance(value, dict) and 'channel' not in value:
        value = {**value, 'channel': 'stable'}
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise protocol.UpdateError('更新网络配置格式无效')
    if not isinstance(value['channel'], str) or value['channel'] not in {'stable', 'preview'}:
        raise protocol.UpdateError('请选择正式版或预览更新通道')
    mode = value['mode']
    if not isinstance(mode, str) or mode not in {'direct', 'http', 'socks5'}:
        raise protocol.UpdateError('请选择直连、HTTP 或 SOCKS5 代理')
    if mode == 'direct':
        return {**DEFAULT, 'channel': value['channel']}
    if not isinstance(value['host'], str):
        raise protocol.UpdateError('代理地址必须是 IPv4 或 IPv6 地址')
    try:
        address = ipaddress.ip_address(value['host'])
    except (ValueError, TypeError):
        raise protocol.UpdateError('代理地址必须是 IPv4 或 IPv6 地址') from None
    if address.is_unspecified or address.is_multicast or address.is_link_local or '%' in str(address):
        raise protocol.UpdateError('代理地址不可使用未指定、多播或链路本地地址')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise protocol.UpdateError('代理端口须为 1–65535')
    for field in ('username', 'password'):
        text = value[field]
        if (not isinstance(text, str) or len(text.encode('utf-8')) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise protocol.UpdateError('代理账号或密码格式无效')
    if ':' in value['username'] or (value['password'] and not value['username']):
        raise protocol.UpdateError('代理账号不可包含冒号，使用密码时须填写账号')
    if mode == 'socks5' and value['username'] and not value['password']:
        raise protocol.UpdateError('SOCKS5 认证须同时填写账号和密码；更换代理地址或账号时请重新填写密码')
    return {**value, 'host': str(address)}


def load(root):
    return validate(protocol.read_json(root / 'network.json', dict(DEFAULT)))


def public(value):
    return {key: item for key, item in value.items() if key != 'password'} | {
        'password_configured': bool(value['password'])}


def save(root, body, current_version):
    fields = set(DEFAULT) - {'password', 'channel'}
    if (not isinstance(body, dict) or not fields <= set(body)
            or set(body) - fields - {'password', 'clear_password', 'channel'}
            or not isinstance(body.get('password', ''), str)
            or type(body.get('clear_password', False)) is not bool
            or (body.get('password') and body.get('clear_password'))):
        raise protocol.UpdateError('更新网络参数无效')
    with protocol.protocol_lock(root):
        previous = protocol.status(root, current_version)
        if previous['phase'] in protocol.BUSY or (root / 'request.json').exists():
            raise protocol.UpdateBusy('更新任务进行中，完成后才能修改更新网络')
        old = load(root)
        value = {key: body[key] for key in fields}
        value['channel'] = body.get('channel', old['channel'])
        # Empty password keeps the existing secret only for the same identity.
        same = all(value[key] == old[key] for key in ('mode', 'host', 'port', 'username'))
        value['password'] = (body.get('password') or (old['password'] if same else ''))
        if body.get('clear_password'):
            value['password'] = ''
            if value['mode'] == 'socks5':
                value['username'] = ''
        value = validate(value)
        # Invalidate confirmation first. A crash must never leave a checked
        # package installable against a newly changed network profile.
        protocol.atomic_json(root / 'status.json', {'phase': 'idle', 'release': None,
            'checked_at': None, 'message': '更新网络已变更，请重新检查更新'})
        protocol.atomic_json(root / 'network.json', value)
        return public(value)


def receive(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise OSError('Proxy closed the connection')
        result.extend(chunk)
    return bytes(result)


def socks_tunnel(sock, host, port, settings):
    authenticated = bool(settings['username'])
    method = 2 if authenticated else 0
    sock.sendall(bytes((5, 1, method)))
    if receive(sock, 2) != bytes((5, method)):
        raise OSError('SOCKS5 authentication method rejected')
    if authenticated:
        user, password = settings['username'].encode('utf-8'), settings['password'].encode('utf-8')
        sock.sendall(bytes((1, len(user))) + user + bytes((len(password),)) + password)
        if receive(sock, 2) != b'\x01\x00':
            raise OSError('SOCKS5 authentication rejected')
    # ATYP=3 delegates destination DNS resolution to the proxy.
    domain = host.encode('idna')
    if not 0 < len(domain) <= 255:
        raise OSError('Invalid SOCKS5 destination')
    sock.sendall(b'\x05\x01\x00\x03' + bytes((len(domain),)) + domain + struct.pack('!H', port))
    version, result, reserved, address_type = receive(sock, 4)
    if version != 5 or result != 0 or reserved != 0:
        raise OSError('SOCKS5 connection rejected')
    if address_type == 1:
        receive(sock, 4)
    elif address_type == 4:
        receive(sock, 16)
    elif address_type == 3:
        receive(sock, receive(sock, 1)[0])
    else:
        raise OSError('Invalid SOCKS5 reply')
    receive(sock, 2)


class TunnelConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, settings, **kwargs):
        self.settings = settings
        super().__init__(host, **kwargs)

    def connect(self):
        settings = self.settings
        sock = None
        try:
            if settings['mode'] == 'http':
                proxy = http.client.HTTPConnection(settings['host'], settings['port'], timeout=self.timeout)
                headers = {}
                if settings['username']:
                    secret = (settings['username'] + ':' + settings['password']).encode('utf-8')
                    headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(secret).decode('ascii')
                proxy.set_tunnel(self.host, self.port, headers=headers)
                try:
                    proxy.connect()
                    sock, proxy.sock = proxy.sock, None
                finally:
                    proxy.close()
            else:
                sock = socket.create_connection((settings['host'], settings['port']), self.timeout)
                socks_tunnel(sock, self.host, self.port, settings)
            # Keep the original destination for SNI and certificate validation.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            if sock is not None:
                sock.close()
            raise


class TunnelHandler(urllib.request.HTTPSHandler):
    def __init__(self, settings, **kwargs):
        self.settings = settings
        super().__init__(**kwargs)

    def https_open(self, request):
        def connect(host, **kwargs):
            return TunnelConnection(host, settings=self.settings, **kwargs)
        return self.do_open(connect, request, context=self._context)


def opener(settings):
    settings = validate(settings)
    handlers = [urllib.request.ProxyHandler({}), protocol.GitHubRedirect()]
    if settings['mode'] != 'direct':
        handlers.append(TunnelHandler(settings))
    return urllib.request.build_opener(*handlers)
