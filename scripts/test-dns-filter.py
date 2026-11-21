"""Exercise the real MosDNS entry with only loopback DNS and private runtime state."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import socketserver
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrated'))

from runtime.dns_filter import FilterManager, core_call
from runtime.filter_rules import parse
from runtime.query_recorder import Recorder
from runtime.query_wire import dns_message

FILTERS = '\n'.join((
    'exact.example.test', '||ads.example.test^', '@@||ok.ads.example.test^',
    '0.0.0.0 hosts.example.test', ':: ipv6hosts.example.test',
    '*-ad.sm.cn*', '*-ad.video.yximgs.com*', '*-ad.wtzw.com*', '*-be-pack-sign.pglstatp-toutiao.com*',
    '@@||ok-ad.sm.cn^',
))

FRAGMENT_CASES = {
    'cdn-ad.sm.cn': True, 'cdn-ad.sm.cn.extra.test': True, 'cdn-ad.sm.cnx': True,
    'cdn-ad.video.yximgs.com': True, 'cdn-ad.wtzw.com': True, 'cdn-be-pack-sign.pglstatp-toutiao.com': True,
    'ad.sm.cn': False, 'cdn-adxsm.cn': False, 'cdn-ad.wtz.com': False, 'ok-ad.sm.cn': False,
}


def wait(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            value = predicate()
        except OSError:
            value = None
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('condition not reached')


def request(name, qtype=1, ident=451):
    question = b''.join(bytes([len(label)]) + label.encode('ascii') for label in name.split('.'))
    return struct.pack('!6H', ident, 256, 1, 0, 0, 0) + question + b'\0' + struct.pack('!HH', qtype, 1)


def exchange(port, name, transport='udp', qtype=1, source='127.0.0.1'):
    wire = request(name, qtype)
    tcp = transport == 'tcp'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.bind((source, 0))
        sock.settimeout(3)
        sock.connect(('127.0.0.1', port))
        sock.sendall(struct.pack('!H', len(wire)) + wire if tcp else wire)
        if tcp:
            with sock.makefile('rb') as stream:
                reply = stream.read(int.from_bytes(stream.read(2), 'big'))
        else:
            reply = sock.recv(4096)
    response = dns_message(reply)
    assert int.from_bytes(wire[:2], 'big') == response['dns_id']
    assert response['response'] and response['domain'] == name.lower() + '.' and response['qtype'] == qtype
    return response


def free_port():
    with socket.socket() as stream, socket.socket(type=socket.SOCK_DGRAM) as datagram:
        stream.bind(('127.0.0.1', 0))
        port = stream.getsockname()[1]
        datagram.bind(('127.0.0.1', port))
        return port


class Authority(socketserver.BaseRequestHandler):
    def handle(self):
        wire, sock = self.request
        query = dns_message(wire)
        name = query['domain']
        self.server.calls[name] += 1
        ident, qtype = query['dns_id'], query['qtype']
        address = {
            1: socket.inet_pton(socket.AF_INET, '192.0.2.123'),
            28: socket.inet_pton(socket.AF_INET6, '2001:db8::123'),
            16: b'\x02ok',
            65: b'\0\1\0',  # priority 1, root target, no parameters
        }.get(qtype, b'')
        code = 5 if name == 'upstream-refusal.example.test.' else 0
        answer = (struct.pack('!HHHIH', 0xc00c, qtype, 1, 360, len(address)) + address) if not code and address else b''
        response = struct.pack('!6H', ident, 0x8180 | code, 1, bool(answer), 0, 0) + wire[12:] + answer
        sock.sendto(response, self.client_address)


@contextmanager
def start(binary, config, run, state, port, wait_dns=True):
    env = {**os.environ, 'LOVESTDNS_FILTER_STATE': str(state / 'dns-filter'), 'LOVESTDNS_FILTER_RUN': str(run)}
    path = run / 'mosdns.log'
    with path.open('wb') as output:
        process = subprocess.Popen([str(binary), 'start', '-d', str(run), '-c', str(config)],
                                   env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            if wait_dns:
                wait(lambda: exchange(port, 'ready.example.test')['rcode'] == 0 and process.poll() is None)
            yield process
        except BaseException:
            sys.stderr.write(path.read_text(encoding='utf-8', errors='replace'))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def config_file(run, port, upstream):
    file = run / 'mosdns.yaml'
    file.write_text(f'''
log:
  level: error
plugins:
  - tag: upstream
    type: forward
    args:
      allowcode: 23
      upstreams:
        - addr: udp://127.0.0.1:{upstream}
  - tag: check_cache
    type: sequence
    args:
      - exec: cache 2048
      - matches: has_wanted_ans
        exec: ok
      - exec: $upstream
  - tag: udp_server
    type: udp_server
    args:
      entry: check_cache
      listen: 127.0.0.1:{port}
  - tag: tcp_server
    type: tcp_server
    args:
      entry: check_cache
      listen: 127.0.0.1:{port}
''', encoding='utf-8')
    return file


def simulate(manager, action, **payload):
    job = manager.submit({'action': action, 'revision': manager.store.revision(), **payload})
    manager.step()
    state = manager.status()
    return state, next(item for item in state['jobs'] if item['id'] == job['id'])


def apply(manager, action, **payload):
    state, job = simulate(manager, action, **payload)
    assert job['phase'] == 'succeeded', job
    return state


def benchmark(port, count):
    exchange(port, 'warm.example.test')
    samples = []
    start_time = time.perf_counter()
    for _ in range(count):
        started = time.perf_counter()
        assert exchange(port, 'warm.example.test')['rcode'] == 0
        samples.append((time.perf_counter() - started) * 1000)
    quantiles = statistics.quantiles(samples, n=100, method='inclusive')
    return {'samples': count, 'qps': round(count / (time.perf_counter() - start_time), 1),
            'p95_ms': round(quantiles[94], 3), 'p99_ms': round(quantiles[98], 3)}


def rss_kib(pid):
    values = Path(f'/proc/{pid}/status').read_text().splitlines()
    return int(next(row for row in values if row.startswith('VmRSS:')).split()[1])


def reference(binary):
    # The reference detects the QNAME decision from reaching the authority.
    # AdGuard Home's response mode is independent of its matching syntax.
    with tempfile.TemporaryDirectory(prefix='ldf-adg-') as directory:
        work = Path(directory)
        server = socketserver.UDPServer(('127.0.0.1', 0), Authority)
        server.calls = Counter()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = free_port()
        config = work / 'AdGuardHome.yaml'
        config.write_text(json.dumps({
            'schema_version': 34, 'http': {'address': f'127.0.0.1:{free_port()}'},
            'dns': {'bind_hosts': ['127.0.0.1'], 'port': port, 'upstream_dns': [f'127.0.0.1:{server.server_address[1]}'],
                    'fallback_dns': [], 'use_private_ptr_resolvers': False, 'cache_enabled': False, 'cache_size': 4096,
                    'ratelimit': 0},
            'filtering': {'protection_enabled': True, 'filtering_enabled': True, 'blocking_mode': 'nxdomain',
                          'filters_update_interval': 0},
            'clients': {'runtime_sources': {'whois': False, 'rdns': False}},
            'filters': [], 'whitelist_filters': [], 'user_rules': FILTERS.splitlines(),
        }), encoding='utf-8')
        try:
            version = subprocess.check_output([str(binary), '--version'], text=True).strip()
            assert version == 'AdGuard Home, version v0.107.76'
            log = work / 'reference.log'
            with log.open('wb') as output:
                process = subprocess.Popen([str(binary), '-c', str(config), '-w', str(work), '--no-check-update',
                                            '--no-permcheck'], stdout=output, stderr=subprocess.STDOUT)
                try:
                    wait(lambda: exchange(port, 'ready.example.test')['rcode'] == 0)
                    expected = {
                        **FRAGMENT_CASES,
                        'exact.example.test': True, 'sub.exact.example.test': False,
                        'ads.example.test': True, 'img.ads.example.test': True, 'badads.example.test': False,
                        'ok.ads.example.test': False, 'child.ok.ads.example.test': False,
                        'hosts.example.test': True, 'child.hosts.example.test': False, 'ipv6hosts.example.test': True,
                    }
                    decisions = {}
                    for transport in ('udp', 'tcp'):
                        for name, block in expected.items():
                            before = server.calls[name + '.']
                            answer = exchange(port, name, transport)
                            assert (server.calls[name + '.'] == before) == block, (transport, name, block, answer)
                            decisions[f'{transport}:{name}'] = 'block' if block else 'pass'
                    return {'reference': version, 'decisions': decisions}
                except BaseException:
                    sys.stderr.write(log.read_text(encoding='utf-8', errors='replace'))
                    raise
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
        finally:
            server.shutdown()
            server.server_close()


def verify(binary, baseline, blocklist, queries):
    with tempfile.TemporaryDirectory(prefix='ldf-') as directory:
        work = Path(directory)
        recorder = Recorder(work, work)
        recorder.core_events.start()
        assert recorder.core_events.listener, 'private event socket unavailable'
        threading.Thread(target=recorder.write_loop, daemon=True).start()
        server = socketserver.UDPServer(('127.0.0.1', 0), Authority)
        server.calls = Counter()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = free_port()
        config = config_file(work, port, server.server_address[1])
        feed = ['! fixture\n||adnet.example.test^\n@@||pass.adnet.example.test^\n']
        manager = FilterManager(work, work, downloader=lambda _: feed[0])
        report = {'platform': platform.platform(), 'timing': {}, 'filter_source': 'real resolver listener'}
        try:
            if baseline:
                with start(baseline, config, work, work, port):
                    before = exchange(port, 'ads.example.test')['rcode']
                    assert before == 0
                    if queries:
                        report['timing']['frozen_source_baseline'] = benchmark(port, queries)
            with start(binary, config, work, work, port) as core:
                wait(lambda: core_call(work, 'status'))
                if queries:
                    report['timing']['disabled'] = {**benchmark(port, queries), 'rss_kib': rss_kib(core.pid)}
                for transport in ('udp', 'tcp'):
                    assert exchange(port, 'ads.example.test', transport)['rcode'] == 0
                upstream_before = server.calls['ads.example.test.']
                state = apply(manager, 'preview', name='Controlled DNS list', url='https://example.test/rules', interval=0)
                state = apply(manager, 'apply_preview', preview=state['draft']['id'], partial=False)
                started = time.perf_counter()
                state = apply(manager, 'configure', enabled=True, local_rules=FILTERS)
                small_apply_ms = round((time.perf_counter() - started) * 1000, 2)
                original_revision = state['revision']
                assert state['live_revision'] == original_revision and state['live_enabled']
                for transport in ('udp', 'tcp'):
                    for qtype in (1, 28, 16, 65):
                        assert exchange(port, 'ADS.example.test', transport, qtype)['rcode'] == 5
                    for name, block in FRAGMENT_CASES.items():
                        assert exchange(port, name, transport)['rcode'] == (5 if block else 0), name
                    for name in ('exact.example.test', 'a.ads.example.test', 'hosts.example.test', 'ipv6hosts.example.test'):
                        assert exchange(port, name, transport)['rcode'] == 5
                    for name in ('sub.exact.example.test', 'badads.example.test', 'ok.ads.example.test',
                                 'child.ok.ads.example.test', 'child.hosts.example.test'):
                        assert exchange(port, name, transport)['rcode'] == 0
                assert server.calls['ads.example.test.'] == upstream_before
                if queries:
                    report['timing']['small'] = {**benchmark(port, queries), 'rss_kib': rss_kib(core.pid), 'apply_ms': small_apply_ms}

                recorder.configure(True)
                endpoints = [{'component': 'mosdns', 'pid': core.pid}]
                epoch = recorder.core_events.renew(endpoints, recorder.generation)
                assert epoch
                with ThreadPoolExecutor(max_workers=4) as clients:
                    results = [clients.submit(exchange, port, 'adnet.example.test', transport, 1, source)
                               for transport in ('udp', 'tcp') for source in ('127.0.0.1', '127.0.0.2')]
                    assert all(task.result()['rcode'] == 5 for task in results)
                # Identical DNS IDs on a single pipelined stream stay distinct.
                wire = request('adnet.example.test')
                frame = struct.pack('!H', len(wire)) + wire
                with socket.create_connection(('127.0.0.1', port), timeout=3) as conn, conn.makefile('rb') as stream:
                    conn.sendall(frame + frame)
                    for _ in range(2):
                        assert dns_message(stream.read(int.from_bytes(stream.read(2), 'big')))['rcode'] == 5
                assert exchange(port, 'upstream-refusal.example.test')['rcode'] == 5
                persisted = wait(lambda: (data if len((data := recorder.store.recent()['events'])) == 7 else None))
                blocked = [event for event in persisted if event['filter']['action'] == 'block']
                assert len(blocked) == len({event['id'] for event in blocked}) == 6
                assert {event['source_ip'] for event in blocked} == {'127.0.0.1', '127.0.0.2'}
                assert {event['transport'] for event in blocked} == {'udp', 'tcp'}
                assert all(event['filter']['source'] == 'Controlled DNS list' and event['filter']['revision'] == original_revision
                           for event in blocked)
                natural = next(event for event in persisted if event['domain'] == 'upstream-refusal.example.test.')
                assert natural['rcode'] == 'REFUSED' and natural['filter']['action'] == 'pass'

                feed[0] = '@@||ads.example.test^$client=192.0.2.4\n||different.example.test^'
                _, failed = simulate(manager, 'refresh', id=state['sources'][0]['id'])
                assert failed['phase'] == 'failed' and manager.store.revision() == original_revision
                assert exchange(port, 'adnet.example.test')['rcode'] == 5
                state = apply(manager, 'allow', domain='adnet.example.test', mode='temporary')
                assert 590 <= state['allow']['adnet.example.test'] - time.time() <= 600
                assert exchange(port, 'adnet.example.test')['addresses'] == ['192.0.2.123']
                assert exchange(port, 'sub.adnet.example.test')['rcode'] == 5
                model = manager.store.model(state['revision'])
                model['allow']['adnet.example.test'] = int(time.time()) + 2
                activated = manager.store.apply(state['revision'], model)
                assert exchange(port, 'adnet.example.test', 'tcp')['rcode'] == 0
                wait(lambda: exchange(port, 'adnet.example.test')['rcode'] == 5, timeout=4)
                assert manager.store.revision() == activated
                assert all(event['filter']['rule'] == '||adnet.example.test^' for event in blocked)
                assert any(event['filter']['revision'] == original_revision for event in recorder.store.recent(decision='block')['events'])

                feed[0] = ('single.adnet.example.test\n||safe.adnet.example.test^\n'
                           '@@||exception.adnet.example.test^\n||conditional.adnet.example.test^$client=127.0.0.1')
                state = apply(manager, 'preview', name='Controlled allow list', url='https://example.test/allow',
                              interval=0, type='allow')
                assert state['draft']['parse']['scopes'] == {'allow_exact': 1, 'allow_suffix': 2}
                assert state['draft']['parse']['unsupported'] == 1
                state = apply(manager, 'apply_preview', preview=state['draft']['id'], partial=True)
                allow_source = state['sources'][-1]['id']
                active_allow = state['revision']
                whitelist_cases = {
                    'single.adnet.example.test': 0, 'child.single.adnet.example.test': 5,
                    'safe.adnet.example.test': 0, 'child.safe.adnet.example.test': 0,
                    'unsafe.adnet.example.test': 5, 'exception.adnet.example.test': 0,
                    'child.exception.adnet.example.test': 0, 'conditional.adnet.example.test': 5,
                }
                for transport in ('udp', 'tcp'):
                    for name, code in whitelist_cases.items():
                        assert exchange(port, name, transport)['rcode'] == code, (transport, name)
                proof = manager.check('child.safe.adnet.example.test')
                assert (proof['action'], proof['source_id'], proof['source'], proof['rule'], proof['revision']) == (
                    'allow', allow_source, 'Controlled allow list', '||safe.adnet.example.test^', active_allow)
                allowed = wait(lambda: [event for event in recorder.store.recent()['events']
                                        if event['filter'].get('source_id') == allow_source])
                assert all(event['filter']['revision'] == active_allow and event['filter']['action'] == 'allow'
                           for event in allowed)
                feed[0] = '@@||conditional.adnet.example.test^$client=127.0.0.1\n||replacement.adnet.example.test^'
                state, failed = simulate(manager, 'refresh', id=allow_source)
                assert failed['phase'] == 'failed' and state['revision'] == active_allow
                assert state['draft']['source_id'] == allow_source
                assert exchange(port, 'child.safe.adnet.example.test')['rcode'] == 0
                assert exchange(port, 'conditional.adnet.example.test')['rcode'] == 5
                state = apply(manager, 'subscription', id=allow_source, enabled=False, remove=False, interval=0)
                assert exchange(port, 'child.safe.adnet.example.test')['rcode'] == 5
                state = apply(manager, 'subscription', id=allow_source, enabled=True, remove=False, interval=0)
                assert exchange(port, 'child.safe.adnet.example.test')['rcode'] == 0
                feed[0] = '||safe.adnet.example.test^'
                state = apply(manager, 'preview', name='Second allow list', url='https://example.test/second',
                              interval=0, type='allow')
                state = apply(manager, 'apply_preview', preview=state['draft']['id'], partial=False)
                replacement = state['sources'][-1]['id']
                state = apply(manager, 'allow', domain='safe.adnet.example.test', mode='permanent')
                state = apply(manager, 'subscription', id=allow_source, enabled=True, remove=True, interval=0)
                assert exchange(port, 'single.adnet.example.test')['rcode'] == 5
                assert exchange(port, 'safe.adnet.example.test')['rcode'] == 0
                assert manager.check('safe.adnet.example.test')['source'] == '本地精确放行'
                assert manager.check('child.safe.adnet.example.test')['source_id'] == replacement
                state = apply(manager, 'allow', domain='safe.adnet.example.test', mode='remove')
                assert manager.check('safe.adnet.example.test')['source_id'] == replacement
                report['allow_subscription'] = {'checks_per_transport': len(whitelist_cases),
                                                'historical_provenance': len(allowed),
                                                'failed_refresh_preserved': True, 'overlap_preserved': True}

                recorder.request({'operation': 'clear'})
                recorder.core_events.renew(endpoints, recorder.generation)
                assert not recorder.store.recent()['events']
                recorder.configure(False)
                assert exchange(port, 'adnet.example.test')['rcode'] == 5
                time.sleep(.15)
                assert not recorder.store.recent()['events']
                report['wire'] = {'blocked_with_evidence': len(blocked), 'natural_refused_is_pass': True,
                                  'upstream_calls_for_warm_blocked_name': server.calls['ads.example.test.'] - upstream_before,
                                  'temporary_allow_seconds': 600, 'expiry_exercised_seconds': 2}

                if queries:
                    feed[0] = '\n'.join(f'||{i:06d}.bulk.example.test^' for i in range(125000))
                    old_source = state['sources'][0]
                    started = time.perf_counter()
                    state = apply(manager, 'refresh', id=old_source['id'])
                    report['timing']['large'] = {**benchmark(port, queries), 'rss_kib': rss_kib(core.pid),
                                                   'apply_ms': round((time.perf_counter() - started) * 1000, 2),
                                                   'rule_count': state['rules']}
                    assert exchange(port, '99999.bulk.example.test')['rcode'] == 0  # leading zero is significant
                    assert exchange(port, '099999.bulk.example.test')['rcode'] == 5
                    assert core_call(work, 'status')['pid'] == core.pid
            (work / 'dns-filter.sock').unlink(missing_ok=True)
            with start(binary, config, work, work, port):
                assert core_call(work, 'status')['revision'] == manager.store.revision()
                assert exchange(port, 'ads.example.test')['rcode'] == 5
                assert manager.check('child.safe.adnet.example.test')['source_id'] == replacement
                for transport in ('udp', 'tcp'):
                    assert exchange(port, 'child.safe.adnet.example.test', transport)['rcode'] == 0
                if not queries:
                    assert exchange(port, 'adnet.example.test')['rcode'] == 5
            (work / 'core-env.json').write_text('{"CNAUTO":"no"}', encoding='utf-8')
            assert manager.status()['mode'] == 'recursive' and not manager.status()['available']
            report['restart_preserved_version'] = True
        finally:
            recorder.stop.set()
            recorder.core_events.listener.close()
            server.shutdown()
            server.server_close()
        if blocklist:
            raw = blocklist.read_bytes()
            _, parsed = parse(raw.decode('utf-8-sig'))
            parsed.pop('issues')
            report['public_list'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), **parsed}
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mosdns', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--list', type=Path)
    parser.add_argument('--adguard-home', type=Path)
    parser.add_argument('--queries', type=int, default=0)
    args = parser.parse_args()
    if not args.mosdns and not args.adguard_home:
        parser.error('Supply --mosdns or --adguard-home')
    result = {}
    if args.adguard_home:
        result['adguard_home'] = reference(args.adguard_home.resolve())
    if args.mosdns:
        result.update(verify(args.mosdns.resolve(), args.baseline.resolve() if args.baseline else None, args.list, args.queries))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
