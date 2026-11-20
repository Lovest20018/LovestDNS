"""Bounded HTTPS list download on the manager thread."""
import concurrent.futures
from contextlib import closing
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

from runtime.filter_rules import MAX_LIST_BYTES

REDIRECTS = {301, 302, 303, 307, 308}
DNS_SLOTS = threading.BoundedSemaphore(2)
NAT64 = (ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('64:ff9b:1::/48'))


def destination(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError('请输入有效的 HTTPS 订阅地址')
    value = urlsplit(url)
    if (value.scheme != 'https' or not value.hostname or value.username is not None
            or value.password is not None or value.fragment or value.port not in (None, 443)
            or '\\' in value.netloc or '%' in value.netloc):
        raise ValueError('订阅地址须使用公网 HTTPS，且只接受默认端口')
    return value


def public_address(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo or any(ip in net for net in NAT64):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def remaining(deadline):
    wait = deadline - time.monotonic()
    if wait <= 0:
        raise TimeoutError
    return wait


def resolve(hostname, deadline):
    if not DNS_SLOTS.acquire(blocking=False):
        raise TimeoutError
    result = concurrent.futures.Future()

    def lookup():
        try:
            result.set_result(socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.set_exception(exc)
        finally:
            DNS_SLOTS.release()

    threading.Thread(target=lookup, daemon=True).start()
    addresses = list(dict.fromkeys(answer[4][0] for answer in result.result(timeout=min(5, remaining(deadline)))))
    if not addresses or not all(public_address(address) for address in addresses):
        raise ValueError('订阅目标必须解析到公网地址')
    return addresses[:8]


class PublicHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, addresses, deadline):
        super().__init__(hostname, 443, timeout=min(5, remaining(deadline)), context=ssl.create_default_context())
        self.addresses = addresses
        self.deadline = deadline

    def connect(self):
        for address in self.addresses:
            try:
                timeout = min(5, remaining(self.deadline))
                with_socket = socket.create_connection((address, 443), timeout)
                try:
                    self.sock = self._context.wrap_socket(with_socket, server_hostname=self.host)
                    return
                except BaseException:
                    with_socket.close()
                    raise
            except ssl.SSLError:
                raise
            except OSError:
                continue
        raise OSError('subscription_connect_failed')


def download(url):
    deadline = time.monotonic() + 20
    try:
        for _ in range(4):
            value = destination(url)
            hostname = value.hostname
            addresses = resolve(hostname, deadline)
            with closing(PublicHTTPS(hostname, addresses, deadline)) as connection:
                path = value.path or '/'
                if value.query:
                    path += '?' + value.query
                connection.request('GET', path, headers={'User-Agent': 'LovestDNS', 'Accept-Encoding': 'identity'})
                peer = connection.sock
                peer.settimeout(min(5, remaining(deadline)))
                # An absolute deadline also covers slow header delivery and chunk framing.
                timer = threading.Timer(remaining(deadline), close_transport, args=(peer,))
                timer.daemon = True
                timer.start()
                try:
                    response = connection.getresponse()
                    if response.status in REDIRECTS and response.getheader('Location'):
                        url = urljoin(url, response.getheader('Location'))
                        continue
                    if response.status != 200:
                        raise ValueError(f'订阅服务器返回 HTTP {response.status}')
                    content_type = response.getheader('Content-Type', '').split(';', 1)[0].lower().strip()
                    if content_type in ('text/html', 'application/xhtml+xml') or response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                        raise ValueError('订阅必须返回未压缩的规则文本')
                    if response.length is not None and response.length > MAX_LIST_BYTES:
                        raise ValueError('订阅超过大小上限')
                    expected = response.length
                    body = response.read(MAX_LIST_BYTES + 1)
                    if len(body) > MAX_LIST_BYTES:
                        raise ValueError('订阅超过大小上限')
                    if expected is not None and len(body) != expected:
                        raise ValueError('订阅下载不完整')
                    remaining(deadline)
                    text = body.decode('utf-8-sig')
                    if text.lstrip().startswith('<') or '\x00' in text:
                        raise ValueError('订阅内容不是可用的规则文本')
                    return text
                finally:
                    timer.cancel()
                    peer.close()
        raise ValueError('订阅地址重定向过多')
    except UnicodeError as exc:
        raise ValueError('订阅内容必须为 UTF-8') from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ValueError('订阅下载失败，请核对地址、证书和网络') from exc


def close_transport(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
