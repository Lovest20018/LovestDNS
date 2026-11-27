"""Opt-in passive recorder. Its queue, database and API never enter DNS routing."""
from collections import Counter
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import socketserver
import sqlite3
import threading
import time
import uuid

from runtime.filter_events import CoreEvents
from runtime.query_capture import PacketSocket, local_addresses
from runtime.query_store import MAX_AGE, Store
from runtime.query_wire import Correlator, MAX_PENDING, MAX_STREAMS, WAIT_SECONDS

MAX_QUEUE = 512
MAX_RESPONSE = 512 * 1024
SUMMARY_CACHE_SECONDS = 3
SUMMARY_WAIT_SECONDS = 1.5
SUMMARY_BUSY = '正在计算解析概览'
SUMMARY_STATE_CHANGED = '查询记录采集状态已改变，请重试'
RECORD_FILTERS = {'limit', 'before', 'source', 'domain', 'qtype', 'exact', 'response', 'start', 'end', 'snapshot', 'decision'}
RECORD_NUMBERS = {'limit': (1, 100), 'before': (1, 2 ** 53 - 1), 'qtype': (0, 65535),
                  'start': (0, 253402300799), 'end': (1, 253402300800), 'snapshot': (0, 2 ** 53 - 1)}
COVERAGE_LOSS = ('kernel_drops', 'queue_drops', 'storage_drops', 'storage_errors', 'pending_evictions',
                 'stream_evictions', 'tcp_gaps', 'tcp_untracked_packets', 'discarded_pending',
                 'discarded_events', 'unparsed_packets', 'invalid_timing', 'capture_errors', 'core_event_drops')


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError('请求必须是对象')
    op = value.get('operation')
    allowed = {'status': {'operation'}, 'recent': {'operation', *RECORD_FILTERS},
               'analytics': {'operation', 'hours'}, 'configure': {'operation', 'enabled'}, 'clear': {'operation'}}
    if not isinstance(op, str) or op not in allowed or set(value) - allowed[op]:
        raise ValueError('查询记录请求无效')
    if op == 'configure' and type(value.get('enabled')) is not bool:
        raise ValueError('enabled 必须是布尔值')
    if op == 'analytics' and (type(value.get('hours')) is not int or value['hours'] not in (1, 24)):
        raise ValueError('统计范围必须是最近 1 小时或 24 小时')
    if op == 'recent':
        for key, (lower, upper) in RECORD_NUMBERS.items():
            if key in value and (type(value[key]) is not int or not lower <= value[key] <= upper):
                raise ValueError(f'{key} 超出范围')
        start, end = value.get('start'), value.get('end')
        if start is not None or end is not None:
            if start is None or end is None or not 0 < end - start <= MAX_AGE:
                raise ValueError('起止时间必须在同一个 24 小时窗口内')
        if 'source' in value:
            if not isinstance(value['source'], str) or '%' in value['source']:
                raise ValueError('来源必须是完整 IP 地址')
            value['source'] = str(ipaddress.ip_address(value['source']))
        for key, size in (('domain', 253), ('exact', 1021)):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > size or any(ord(c) < 32 for c in value[key])):
                raise ValueError(f'域名筛选最多 {size} 个字符')
        if 'exact' in value:
            value['exact'] = value['exact'].lower() if value['exact'] == '.' else value['exact'].lower().removesuffix('.')
        if 'response' in value and (not isinstance(value['response'], str)
                                     or not re.fullmatch(r'no_response_observed|[A-Z0-9_]{1,20}', value['response'])):
            raise ValueError('响应筛选无效')
        if 'decision' in value and value['decision'] not in ('block', 'allow', 'pass'):
            raise ValueError('过滤判定筛选无效')
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


class SummaryFlight:
    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.error = None


class Recorder:
    def __init__(self, state, run):
        self.state, self.run = state, run
        # When both are needed, acquire the storage lock before the state lock.
        self.storage_lock = threading.Lock()
        self.lock = threading.RLock()
        self.events = queue.Queue(MAX_QUEUE)
        self.counters = Counter()
        self.session = uuid.uuid4().hex
        self.generation = 0
        self.clearing = False
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
        self.core_events = CoreEvents(self)
        self.core_active = False
        self.summary_slot = threading.BoundedSemaphore(1)
        self.summary_cache = {}
        self.summary_flights = {}

    def configure(self, enabled):
        # Complete any admitted write before acknowledging the new capture state.
        with self.storage_lock, self.lock:
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
                self.summary_cache.clear()
                self.core_events.session = None
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
                    'source': 'mosdns_entry_events' if self.core_active else 'linux_passive_entry_packets',
                    'scope': 'service_network_namespace',
                    'timing_scope': 'mosdns_entry_processing' if self.core_active else 'passive_entry_packet_interval',
                    'endpoints': list(self.endpoints),
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
            with self.storage_lock:
                with self.lock:
                    self.generation += 1
                    self.summary_cache.clear()
                    self.core_events.session = None
                    self.clearing = True
                try:
                    self.store.clear()
                finally:
                    with self.lock:
                        self.clearing = False
        elif op == 'recent':
            return {'status': self.status(), **self.store.recent(**request)}
        elif op == 'analytics':
            return self.analytics(request['hours'])
        return self.status()

    def analytics(self, hours):
        with self.lock:
            if self.clearing:
                raise RuntimeError(SUMMARY_STATE_CHANGED)
            generation = self.generation
            cached = self.summary_cache.get(hours)
            if cached and time.monotonic() - cached[0] < SUMMARY_CACHE_SECONDS:
                return {'status': self.status(), **cached[1]}
            key = (generation, hours)
            flight = self.summary_flights.get(key)
            leader = flight is None
            if leader:
                if not self.summary_slot.acquire(blocking=False):
                    raise RuntimeError(SUMMARY_BUSY)
                flight = self.summary_flights[key] = SummaryFlight()

        if leader:
            try:
                flight.result = self.store.analytics(hours)
            except BaseException as exc:
                flight.error = exc
            finally:
                with self.lock:
                    if flight.error is None and self.generation == generation:
                        self.summary_cache[hours] = (time.monotonic(), flight.result)
                    del self.summary_flights[key]
                    self.summary_slot.release()
                    flight.done.set()
        elif not flight.done.wait(SUMMARY_WAIT_SECONDS):
            raise RuntimeError(SUMMARY_BUSY)

        with self.lock:
            if self.generation != generation:
                raise RuntimeError(SUMMARY_STATE_CHANGED)
            if flight.error is not None:
                raise flight.error
            return {'status': self.status(), **flight.result}

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
                with self.storage_lock:
                    with self.lock:
                        events = [event for generation, event in batch if generation == self.generation and self.enabled]
                        self.counters['discarded_events'] += len(batch) - len(events)
                    if events:
                        self.store.append(events)
                        with self.lock:
                            self.counters['stored_events'] += len(events)
            except queue.Empty:
                pass
            except (OSError, ValueError, sqlite3.Error):
                self.counters['storage_drops'] += len(batch)
            if time.monotonic() >= deadline:
                try:
                    with self.storage_lock:
                        self.store.prune()
                except (OSError, sqlite3.Error):
                    self.counters['storage_errors'] += 1
                deadline = time.monotonic() + 5

    def capture_loop(self):
        capture, correlator, signature = None, None, None
        deadline = 0
        clock_offset = time.time() - time.monotonic()
        streak_start, last_check, recorded_until, previous_loss = None, None, None, None
        invalidate_after = None
        try:
            while not self.stop.is_set():
                mono = time.monotonic()
                if mono >= deadline:
                    deadline = mono + 1
                    try:
                        with self.lock:
                            enabled, generation = self.enabled, self.generation
                        endpoints = entry_endpoints(self.run) if enabled else []
                        try:
                            core_epoch = self.core_events.renew(endpoints, generation)
                        except (OSError, ValueError):
                            core_epoch = None
                        addresses = local_addresses() if enabled and core_epoch is None else set()
                        current = (generation, core_epoch, json.dumps(endpoints, sort_keys=True), tuple(sorted(addresses)))
                        if current != signature:
                            streak_start, recorded_until = None, None
                            if capture:
                                capture.close()
                                capture = None
                            if correlator:
                                correlator.reset()
                            if enabled and core_epoch is None:
                                capture = PacketSocket([e['port'] for e in endpoints])
                                correlator = Correlator(endpoints, lambda event, generation=generation: self.emit(generation, event),
                                                        self.counters, f'{self.session}:{generation}:{uuid.uuid4().hex}', addresses)
                            signature = current
                        # Entry validation can block. Pair fresh clocks here so
                        # its duration cannot look like a wall-clock step.
                        offset = time.time() - time.monotonic()
                        clock_step = abs(offset - clock_offset) > .1
                        if clock_step and correlator:
                            correlator.reset()
                            self.counters['clock_resets'] += 1
                        if capture:
                            drops = capture.drops()
                            if drops:
                                self.counters['kernel_drops'] += drops
                                correlator.reset()
                        coverage = None
                        with self.lock:
                            now = time.time()
                            loss = tuple(self.counters[key] for key in COVERAGE_LOSS)
                            if clock_step and offset < clock_offset:
                                cutoff = int(now)
                                invalidate_after = cutoff if invalidate_after is None else min(invalidate_after, cutoff)
                            if (streak_start is None or last_check is None or mono - last_check > 2
                                    or clock_step or loss != previous_loss):
                                streak_start, recorded_until = int(now) + 1, None
                            last_check, previous_loss = mono, loss
                            if generation == self.generation:
                                self.core_active = core_epoch is not None
                                self.phase = 'recording' if enabled else 'disabled'
                                self.reason, self.endpoints, self.updated_at = None, endpoints, now
                                if enabled:
                                    covered_until = int(now) - WAIT_SECONDS - 2
                                    left = recorded_until if recorded_until is not None else streak_start
                                    if covered_until - left >= 5:
                                        coverage = left, covered_until
                        clock_offset = offset
                        # Coverage can be retried at the next sample. Packet capture
                        # should keep running while the writer holds the storage lock.
                        if (invalidate_after is not None or coverage) and self.storage_lock.acquire(blocking=False):
                            try:
                                if invalidate_after is not None:
                                    self.store.invalidate_coverage_after(invalidate_after)
                                    invalidate_after = None
                                with self.lock:
                                    still_recording = self.enabled and generation == self.generation
                                if coverage and still_recording:
                                    self.store.record_coverage(*coverage)
                                    recorded_until = coverage[1]
                            finally:
                                self.storage_lock.release()
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        if capture:
                            capture.close()
                        if correlator:
                            correlator.reset()
                        capture, correlator, signature = None, None, None
                        streak_start, recorded_until = None, None
                        with self.lock:
                            self.phase, self.reason, self.endpoints = 'unavailable', 'entry_or_capture_unavailable', []
                            self.core_active = False
                            self.core_events.session = None
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
    recorder.core_events.start()
    writer = threading.Thread(target=recorder.write_loop, daemon=True)
    capture = threading.Thread(target=recorder.capture_loop, daemon=True)
    writer.start()
    capture.start()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            raw = self.rfile.readline(8193)
            try:
                if len(raw) > 8192 or not raw.endswith(b'\n'):
                    raise ValueError('请求过大')
                result = recorder.request(json.loads(raw))
                response = {'ok': True, 'result': result}
            except (ValueError, TypeError, OSError, sqlite3.Error, RuntimeError):
                response = {'ok': False, 'error': '查询记录服务暂不可用或请求无效'}
            payload = json.dumps(response, allow_nan=False).encode() + b'\n'
            if len(payload) > MAX_RESPONSE:
                payload = b'{"ok":false,"error":"response_limit"}\n'
            self.wfile.write(payload)

    path = run / 'query-recorder.sock'
    path.unlink(missing_ok=True)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        slots = threading.BoundedSemaphore(4)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.slots.release()

    try:
        with Server(str(path), Handler) as service:
            path.chmod(0o600)
            service.serve_forever(poll_interval=.2)
    finally:
        recorder.stop.set()
        writer.join()
        capture.join()
        recorder.store.close()


if __name__ == '__main__':
    main()
