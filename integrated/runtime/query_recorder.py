"""Opt-in passive recorder. Its queue, database and API never enter DNS routing."""
from collections import Counter
import ipaddress
import json
import os
from pathlib import Path
import queue
import socket
import socketserver
import sqlite3
import threading
import time
import uuid

from runtime.query_capture import PacketSocket, local_addresses
from runtime.query_store import Store
from runtime.query_wire import Correlator, MAX_PENDING, MAX_STREAMS, WAIT_SECONDS

MAX_QUEUE = 512
MAX_RESPONSE = 512 * 1024


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError('请求必须是对象')
    op = value.get('operation')
    allowed = {'status': {'operation'}, 'recent': {'operation', 'limit', 'before', 'source', 'domain', 'qtype'},
               'configure': {'operation', 'enabled'}, 'clear': {'operation'}}
    if op not in allowed or set(value) - allowed[op]:
        raise ValueError('查询记录请求无效')
    if op == 'configure' and type(value.get('enabled')) is not bool:
        raise ValueError('enabled 必须是布尔值')
    if op == 'recent':
        for key, lower, upper in [('limit', 1, 100), ('before', 1, 2 ** 63 - 1), ('qtype', 0, 65535)]:
            if key in value and (type(value[key]) is not int or not lower <= value[key] <= upper):
                raise ValueError(f'{key} 超出范围')
        if 'source' in value:
            value['source'] = str(ipaddress.ip_address(value['source']))
        if 'domain' in value and (not isinstance(value['domain'], str) or len(value['domain']) > 253 or any(ord(c) < 32 for c in value['domain'])):
            raise ValueError('域名筛选最多 253 个字符')
    return value


def entry_endpoints(run):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(.5)
        sock.connect(str(run / 'observer.sock'))
        sock.sendall(b'snapshot\n')
        with sock.makefile('rb') as source:
            raw = source.readline(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE or not raw.endswith(b'\n'):
        raise ValueError('observer_unavailable')
    sample = json.loads(raw)
    if not 0 <= time.time() - sample.get('sampled_at', 0) <= 10:
        raise ValueError('observer_stale')
    dns = sample.get('dns', {})
    component = dns.get('entry_component')
    observation = dns.get('listeners', {}).get(component, {})
    if component not in {'mosdns', 'unbound-raw'} or not observation.get('available'):
        raise ValueError('entry_unconfirmed')
    # Refuse cached observations after PID reuse or a component restart.
    from runtime.dns_listeners import observe_process
    fresh = observe_process({'statename': 'RUNNING', 'pid': observation['pid']})
    if not fresh.get('available') or fresh.get('start_ticks') != observation.get('start_ticks'):
        raise ValueError('entry_changed')
    owned = {(e['address'], e['port'], e['transport']) for e in fresh['endpoints']}
    endpoints = [dict(e, component=component, pid=observation['pid'], start_ticks=observation['start_ticks'])
                 for e in observation['endpoints'] if (e['address'], e['port'], e['transport']) in owned]
    if not endpoints or len(endpoints) > 16:
        raise ValueError('entry_unconfirmed')
    return endpoints


class Recorder:
    def __init__(self, state, run):
        self.state, self.run = state, run
        self.lock = threading.RLock()
        self.events = queue.Queue(MAX_QUEUE)
        self.counters = Counter()
        self.session = uuid.uuid4().hex
        self.generation = 0
        self.enabled = False
        self.phase, self.reason, self.endpoints = 'disabled', None, []
        self.updated_at = time.time()
        self.stop = threading.Event()
        self.settings = state / 'query-recording.json'
        if self.settings.exists():
            if self.settings.is_symlink() or self.settings.stat().st_size > 1024:
                raise ValueError('invalid_query_settings')
            settings = json.loads(self.settings.read_text())
            self.enabled = validate_request({'operation': 'configure', **settings})['enabled']
        self.store = Store(state / 'queries')

    def configure(self, enabled):
        with self.lock:
            if self.settings.is_symlink():
                raise ValueError('查询采集设置不可写')
            temporary = self.settings.with_name('.query-recording-' + uuid.uuid4().hex)
            try:
                with temporary.open('x', encoding='utf-8') as target:
                    temporary.chmod(0o600)
                    target.write(json.dumps({'enabled': enabled}))
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.settings)
            finally:
                temporary.unlink(missing_ok=True)
            if self.enabled != enabled:
                self.generation += 1
            self.enabled = enabled
            self.phase = 'starting' if enabled else 'disabled'
            self.reason = None
            self.updated_at = time.time()

    def status(self):
        with self.lock:
            stale = self.enabled and not 0 <= time.time() - self.updated_at <= 5
            phase = 'unavailable' if stale else self.phase
            return {'enabled': self.enabled, 'active': self.enabled and phase == 'recording', 'phase': phase,
                    'reason': 'recorder_stale' if stale else self.reason, 'sampled_at': self.updated_at, 'session_id': self.session,
                    'source': 'linux_passive_entry_packets', 'scope': 'service_network_namespace',
                    'timing_scope': 'passive_entry_packet_interval', 'endpoints': list(self.endpoints),
                    'counters': dict(self.counters), 'queue_depth': self.events.qsize(),
                    'memory_limits': {'event_queue': MAX_QUEUE, 'pending_queries': MAX_PENDING,
                                      'tcp_streams': MAX_STREAMS, 'response_wait_seconds': WAIT_SECONDS},
                    **self.store.summary()}

    def request(self, request):
        request = validate_request(dict(request))
        op = request.pop('operation')
        if op == 'configure':
            self.configure(request['enabled'])
        elif op == 'clear':
            with self.lock:
                self.generation += 1
                self.store.clear()
        elif op == 'recent':
            return {'status': self.status(), **self.store.recent(**request)}
        return self.status()

    def emit(self, generation, event):
        try:
            self.events.put_nowait((generation, event))
        except queue.Full:
            self.counters['queue_drops'] += 1

    def write_loop(self):
        deadline = 0
        while not self.stop.is_set():
            try:
                generation, event = self.events.get(timeout=.2)
                batch = [(generation, event)]
                batch_deadline = time.monotonic() + .02
                while len(batch) < 128:
                    try:
                        remaining = batch_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        batch.append(self.events.get(timeout=remaining))
                    except queue.Empty:
                        break
                with self.lock:
                    events = [event for generation, event in batch if generation == self.generation and self.enabled]
                    self.counters['discarded_events'] += len(batch) - len(events)
                    if events:
                        self.store.append(events)
                        self.counters['stored_events'] += len(events)
            except queue.Empty:
                pass
            except (OSError, ValueError, sqlite3.Error):
                self.counters['storage_drops'] += len(batch)
            if time.monotonic() >= deadline:
                try:
                    self.store.prune()
                except (OSError, sqlite3.Error):
                    self.counters['storage_errors'] += 1
                deadline = time.monotonic() + 5

    def capture_loop(self):
        capture, correlator, signature = None, None, None
        deadline = 0
        clock_offset = time.time() - time.monotonic()
        try:
            while not self.stop.is_set():
                mono = time.monotonic()
                if mono >= deadline:
                    deadline = mono + 1
                    try:
                        with self.lock:
                            enabled, generation = self.enabled, self.generation
                        endpoints = entry_endpoints(self.run) if enabled else []
                        addresses = local_addresses() if enabled else set()
                        current = (generation, json.dumps(endpoints, sort_keys=True), tuple(sorted(addresses)))
                        if current != signature:
                            if capture:
                                capture.close()
                                capture = None
                            if correlator:
                                correlator.reset()
                            if enabled:
                                capture = PacketSocket([e['port'] for e in endpoints])
                                correlator = Correlator(endpoints, lambda event, generation=generation: self.emit(generation, event),
                                                        self.counters, f'{self.session}:{generation}:{uuid.uuid4().hex}', addresses)
                            signature = current
                        # Entry validation can block. Pair fresh clocks here so
                        # its duration cannot look like a wall-clock step.
                        offset = time.time() - time.monotonic()
                        if abs(offset - clock_offset) > .1 and correlator:
                            correlator.reset()
                            self.counters['clock_resets'] += 1
                        clock_offset = offset
                        if capture:
                            drops = capture.drops()
                            if drops:
                                self.counters['kernel_drops'] += drops
                                correlator.reset()
                        with self.lock:
                            self.phase = 'recording' if enabled else 'disabled'
                            self.reason, self.endpoints, self.updated_at = None, endpoints, time.time()
                    except (OSError, ValueError, KeyError, TypeError):
                        if capture:
                            capture.close()
                        if correlator:
                            correlator.reset()
                        capture, correlator, signature = None, None, None
                        with self.lock:
                            self.phase, self.reason, self.endpoints = 'unavailable', 'entry_or_capture_unavailable', []
                            self.updated_at = time.time()
                if capture is None:
                    self.stop.wait(.2)
                    continue
                try:
                    received = capture.receive()
                    if received:
                        packet, wall = received
                        correlator.feed(packet, wall, time.monotonic())
                    correlator.expire(time.monotonic())
                except socket.timeout:
                    correlator.expire(time.monotonic())
                except ValueError:
                    self.counters['capture_errors'] += 1
                except OSError:
                    capture.close()
                    correlator.reset()
                    capture, signature = None, None
                    with self.lock:
                        self.phase, self.reason = 'unavailable', 'capture_failed'
        finally:
            if capture:
                capture.close()


def main():
    state = Path(os.environ.get('SUITE_STATE_DIR', '/var/lib/paopaodns-suite'))
    run = Path(os.environ.get('SUITE_RUN_DIR', '/run/paopaodns-suite'))
    os.umask(0o077)
    recorder = Recorder(state, run)
    threading.Thread(target=recorder.write_loop, daemon=True).start()
    threading.Thread(target=recorder.capture_loop, daemon=True).start()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            raw = self.rfile.readline(2049)
            try:
                if len(raw) > 2048 or not raw.endswith(b'\n'):
                    raise ValueError('请求过大')
                result = recorder.request(json.loads(raw))
                response = {'ok': True, 'result': result}
            except (ValueError, TypeError, OSError, sqlite3.Error):
                response = {'ok': False, 'error': '查询记录服务暂不可用或请求无效'}
            payload = json.dumps(response, allow_nan=False).encode() + b'\n'
            if len(payload) > MAX_RESPONSE:
                payload = b'{"ok":false,"error":"response_limit"}\n'
            self.wfile.write(payload)

    path = run / 'query-recorder.sock'
    path.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(path), Handler) as service:
        path.chmod(0o600)
        service.serve_forever(poll_interval=.2)


if __name__ == '__main__':
    main()
