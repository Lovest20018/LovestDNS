"""Bounded local DNS fixtures and component processes for compatibility tests."""
from collections import Counter
import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import random
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time

from dns_cpu import pin, placed


def question(name, qtype=1, identifier=None):
    wire = b''.join(bytes([len(label)]) + label.encode('ascii') for label in name.rstrip('.').split('.')) + b'\0'
    return struct.pack('!HHHHHH', random.randrange(65536) if identifier is None else identifier, 0x100, 1, 0, 0, 0) + wire + struct.pack('!HH', qtype, 1)


def read_name(packet, offset):
    labels, end, seen = [], None, set()
    while True:
        if offset in seen or offset >= len(packet):
            raise ValueError('Invalid DNS name')
        seen.add(offset)
        length = packet[offset]
        if length & 0xc0 == 0xc0:
            pointer = ((length & 0x3f) << 8) | packet[offset + 1]
            end = end or offset + 2
            offset = pointer
            continue
        offset += 1
        if not length:
            return '.'.join(labels), end or offset
        if length > 63 or offset + length > len(packet):
            raise ValueError('Invalid DNS label')
        labels.append(packet[offset:offset + length].decode('ascii'))
        offset += length


def parse(packet):
    identifier, flags, qd, an, _, _ = struct.unpack('!HHHHHH', packet[:12])
    offset = 12
    for _ in range(qd):
        _, offset = read_name(packet, offset)
        offset += 4
    answers = []
    for _ in range(an):
        name, offset = read_name(packet, offset)
        kind, cls, ttl, size = struct.unpack('!HHIH', packet[offset:offset + 10])
        offset += 10
        raw = packet[offset:offset + size]
        if len(raw) != size:
            raise ValueError('Truncated DNS answer')
        value = (socket.inet_ntop(socket.AF_INET if kind == 1 else socket.AF_INET6, raw) if kind in (1, 28)
                 else read_name(packet, offset)[0] if kind == 5 else raw.hex())
        answers.append({'name': name, 'type': kind, 'class': cls, 'ttl': ttl, 'value': value})
        offset += size
    return {'id': identifier, 'rcode': flags & 15, 'truncated': bool(flags & 0x200), 'answers': answers}


def exact(stream, size):
    result = bytearray()
    while len(result) < size:
        data = stream.recv(size - len(result))
        if not data:
            raise OSError('Truncated TCP message')
        result.extend(data)
    return bytes(result)


def query(port, name, *, qtype=1, timeout=2, tcp=False):
    packet = question(name, qtype)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.connect(('127.0.0.1', port))
        if tcp:
            client.sendall(struct.pack('!H', len(packet)) + packet)
            raw = exact(client, struct.unpack('!H', exact(client, 2))[0])
        else:
            client.send(packet)
            raw = client.recv(65535)
    result = parse(raw)
    if result['id'] != int.from_bytes(packet[:2], 'big'):
        raise ValueError('DNS transaction mismatch')
    return result


class Fixture:
    def __init__(self, address='192.0.2.10', ttl=120, *, threaded_udp=True, track_questions=False):
        self.address, self.ttl = address, ttl
        self.counts = Counter()
        self.question_counts = Counter() if track_questions else None
        self.lock = threading.Lock()
        self.delay = 0
        owner = self

        class UDP(socketserver.BaseRequestHandler):
            def handle(self):
                packet, stream = self.request
                stream.sendto(owner.respond(packet, False), self.client_address)

        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                try:
                    packet = exact(self.request, struct.unpack('!H', exact(self.request, 2))[0])
                    response = owner.respond(packet, True)
                    self.request.sendall(struct.pack('!H', len(response)) + response)
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

        self.tcp = Server(('127.0.0.1', 0), TCP)
        self.port = self.tcp.server_address[1]
        udp_server = socketserver.ThreadingUDPServer if threaded_udp else socketserver.UDPServer
        self.udp = udp_server(('127.0.0.1', self.port), UDP)
        self.udp.daemon_threads = True
        for server in (self.tcp, self.udp):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def respond(self, packet, tcp):
        name, end = read_name(packet, 12)
        qtype, qclass = struct.unpack('!HH', packet[end:end + 4])
        with self.lock:
            self.counts[(name, 'tcp' if tcp else 'udp')] += 1
            if self.question_counts is not None:
                self.question_counts[(name.lower(), qtype, qclass, 'tcp' if tcp else 'udp')] += 1
        if self.delay:
            time.sleep(self.delay)
        missing = name.startswith('missing.') or (bool(name) and not name.endswith('.cache.test'))
        truncated = name.startswith('trunc.') and not tcp
        flags = 0x8583 if missing else 0x8780 if truncated else 0x8580
        answer, answer_count = b'', 0
        if not missing and not truncated and qtype in (1, 28):
            value = socket.inet_aton(self.address) if qtype == 1 else socket.inet_pton(socket.AF_INET6, '2001:db8::10')
            ttl = 2 if name.startswith('short.') else self.ttl
            answer = b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, ttl, len(value)) + value
            answer_count = 1
            if name.startswith('cname.'):
                target = b'\x06target\x05cache\x04test\0'
                answer = (b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, ttl, len(target)) + target
                          + target + struct.pack('!HHIH', qtype, 1, ttl, len(value)) + value)
                answer_count = 2
        return struct.pack('!HHHHHH', int.from_bytes(packet[:2], 'big'), flags, 1, answer_count, 0, 0) + packet[12:end + 4] + answer

    def requests(self):
        with self.lock:
            return sum(self.counts.values())

    def question_snapshot(self):
        with self.lock:
            if self.question_counts is None:
                raise RuntimeError('Question tracking was not enabled for this fixture')
            return self.question_counts.copy()

    def close(self):
        for server in (self.tcp, self.udp):
            server.shutdown()
            server.server_close()


def fixture_worker(connection, arguments, cpu):
    fixture = None
    try:
        pin(cpu)
        fixture = Fixture(**arguments)
        connection.send({'ready': True, 'port': fixture.port,
                         'affinity': sorted(os.sched_getaffinity(0))})
        while True:
            operation = connection.recv()
            if operation == 'stop':
                break
            if operation == 'requests':
                connection.send(fixture.requests())
            elif operation == 'questions':
                connection.send(fixture.question_snapshot())
            else:
                raise ValueError('Unknown fixture control operation')
    except Exception as error:
        connection.send({'fixture_error': repr(error)})
    finally:
        if fixture is not None:
            fixture.close()
        connection.close()


class FixtureProcess:
    """Keep the controlled upstream on its own interpreter and optional CPU."""
    def __init__(self, *, cpu=None, **arguments):
        self.address = arguments.get('address', '192.0.2.10')
        context = multiprocessing.get_context('spawn')
        self.connection, peer = context.Pipe()
        self.process = context.Process(target=fixture_worker, args=(peer, arguments, cpu), daemon=True)
        self.process.start()
        peer.close()
        try:
            metadata = self.receive()
            if not metadata.get('ready'):
                raise RuntimeError('Controlled upstream did not become ready')
            self.port, self.affinity = metadata['port'], metadata['affinity']
        except BaseException:
            self.close()
            raise

    def receive(self):
        if not self.connection.poll(10):
            raise TimeoutError('Controlled upstream control response timed out')
        result = self.connection.recv()
        if isinstance(result, dict) and 'fixture_error' in result:
            raise RuntimeError(result['fixture_error'])
        return result

    def requests(self):
        self.connection.send('requests')
        return self.receive()

    def question_snapshot(self):
        self.connection.send('questions')
        return self.receive()

    def close(self):
        try:
            if self.process.is_alive():
                self.connection.send('stop')
                self.process.join(timeout=3)
        except (OSError, EOFError):
            pass
        finally:
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            self.connection.close()


class Child:
    def __init__(self, argv, log, *, cpu=None):
        self.log = Path(log)
        self.stream = self.log.open('ab')
        self.process = subprocess.Popen(placed([str(x) for x in argv], cpu), stdout=self.stream, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL)

    def check(self):
        if self.process.poll() is not None:
            raise RuntimeError('Component exited: ' + self.log.read_text(errors='replace')[-3000:])

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.stream.close()


def free_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def candidate_command(bundle, component, binary):
    bundle = Path(bundle).resolve()
    metadata = json.loads((bundle / 'manifest.json').read_text())['components'][component]
    base = bundle / component
    runtime = metadata['runtime']
    if runtime.get('loader'):
        loader = base / runtime['loader']
        loader.chmod(0o755)
        return [str(loader), '--library-path', str(base / runtime['library_path']), str(base / metadata['binaries'][binary])]
    return [str(base / metadata['binaries'][binary])]


class CacheLab:
    def __init__(self, root, redis_command, cli_command, unbound_command, fixtures, *, command_timeout=None,
                 cpu_layout=None, serve_expired_client_timeout=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cli_command, self.unbound_command = cli_command, unbound_command
        self.fixtures = fixtures
        self.cpu_layout = cpu_layout or {}
        self.socket_directory = tempfile.TemporaryDirectory(prefix='ldns-lab-')
        self.socket = Path(self.socket_directory.name) / 'r.sock'
        self.children, self.unbounds, self.ports = [], [], []
        self.redis_command = [*redis_command, '--port', '0', '--unixsocket', str(self.socket), '--databases', '2',
                            '--dir', str(self.root), '--dbfilename', 'cache.rdb', '--save', '', '--appendonly', 'no',
                            '--maxmemory', '64mb', '--maxmemory-policy', 'allkeys-lru']
        self.start_redis()

        for db, fixture in enumerate(fixtures):
            self.configure_unbound(db, fixture, command_timeout, serve_expired_client_timeout)

    def start_redis(self):
        self.redis = Child(self.redis_command, self.root / 'redis.log', cpu=self.cpu_layout.get('redis'))
        self.children.append(self.redis)
        self.wait(lambda: self.cli('PING') == 'PONG')

    def configure_unbound(self, db, fixture, command_timeout, serve_expired_client_timeout):
        port = free_port()
        self.ports.append(port)
        config = self.root / f'unbound-{db}.conf'
        value = f'''server:
    interface: 127.0.0.1
    port: {port}
    username: ""
    chroot: ""
    directory: "{self.root}"
    pidfile: "{self.root}/u{db}.pid"
    use-syslog: no
    verbosity: 0
    do-ip6: no
    do-not-query-localhost: no
    local-zone: "test." transparent
    num-threads: 1
    module-config: "cachedb iterator"
    cache-max-ttl: 600
    cache-max-negative-ttl: 2
    prefetch: yes
    serve-expired: yes
    serve-expired-ttl: 0
    serve-expired-reply-ttl: 0
cachedb:
    backend: "redis"
    secret-seed: "compatibility-test-seed"
    redis-server-path: "{self.socket}"
    redis-logical-db: {db}
    redis-timeout: 100
    redis-expire-records: no
    cachedb-check-when-serve-expired: no
forward-zone:
    name: "."
    forward-addr: 127.0.0.1@{fixture.port}
'''
        if command_timeout is not None:
            value = value.replace('    redis-timeout: 100', f'    redis-timeout: 100\n    redis-command-timeout: {command_timeout}\n    redis-connect-timeout: 100')
        if serve_expired_client_timeout is not None:
            value = value.replace('    serve-expired: yes\n',
                                  f'    serve-expired: yes\n    serve-expired-client-timeout: {serve_expired_client_timeout}\n')
        config.write_text(value)
        self.start_unbound(db)

    def start_unbound(self, db):
        child = Child([*self.unbound_command, '-d', '-p', '-c', self.root / f'unbound-{db}.conf'], self.root / f'unbound-{db}.log',
                      cpu=self.cpu_layout.get('unbound'))
        self.children.append(child)
        if db < len(self.unbounds):
            self.unbounds[db] = child
        else:
            self.unbounds.append(child)
        # A TCP connect checks readiness without warming a DNS cache entry.
        def ready():
            with socket.create_connection(('127.0.0.1', self.ports[db]), timeout=.2):
                return True
        self.wait(ready)

    def wait(self, probe):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for child in self.children:
                child.check()
            try:
                if probe():
                    return
            except (OSError, subprocess.SubprocessError):
                pass
            time.sleep(.05)
        raise TimeoutError('Local component readiness timed out')

    def cli(self, *args, db=0):
        return subprocess.check_output([*self.cli_command, '-s', str(self.socket), '-n', str(db), '--raw', *args],
                                       text=True, stderr=subprocess.STDOUT, timeout=5).strip()

    def mosdns_proxy(self, command):
        """Start the frozen MosDNS cache/forward path on another isolated port."""
        port = free_port()
        config = self.root / 'mosdns.json'
        value = {'log': {'level': 'error'}, 'plugins': [
            {'tag': 'upstream', 'type': 'forward', 'args': {'upstreams': [{'addr': f'udp://127.0.0.1:{self.ports[0]}'}]}},
            {'tag': 'respond', 'type': 'sequence', 'args': [{'exec': 'ok'}]},
            {'tag': 'main', 'type': 'sequence', 'args': [{'exec': 'cache 4096'},
                {'matches': 'has_wanted_ans', 'exec': 'goto respond'}, {'exec': '$upstream'}]},
            *[{'tag': kind, 'type': kind, 'args': {'entry': 'main', 'listen': f'127.0.0.1:{port}'}}
              for kind in ('udp_server', 'tcp_server')]]}
        config.write_text(json.dumps(value))
        self.mosdns = Child([*command, 'start', '-c', config], self.root / 'mosdns.log')
        self.children.append(self.mosdns)
        def ready():
            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                return True
        self.wait(ready)
        return port

    def restart_unbounds(self):
        for child in self.unbounds:
            child.close()
            self.children.remove(child)
        for db in range(len(self.fixtures)):
            self.start_unbound(db)

    def restart_all(self):
        """Reload the saved RDB, discarding both Redis and Unbound memory."""
        for child in reversed(self.children):
            child.close()
        self.children.clear()
        self.unbounds.clear()
        self.start_redis()
        for db in range(len(self.fixtures)):
            self.start_unbound(db)

    def close(self):
        for child in reversed(self.children):
            child.close()
        self.children.clear()
        if getattr(self, 'socket_directory', None):
            self.socket_directory.cleanup()


@contextlib.contextmanager
def running_lab(*args, **kwargs):
    lab = CacheLab.__new__(CacheLab)
    lab.children = []
    try:
        lab.__init__(*args, **kwargs)
        yield lab
    finally:
        lab.close()
