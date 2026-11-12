"""Isolated Linux acceptance using a supplied frozen MosDNS, never live DNS ports."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import math
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrated'))
from runtime.dns_listeners import process_start


def query(port, *, transport='udp', domain='recording.test', kind=1, ipv6=False, ident=1):
    wire = struct.pack('!6H', ident, 256, 1, 0, 0, 0) + b''.join(bytes([len(p)]) + p.encode() for p in domain.split('.')) + b'\0' + struct.pack('!HH', kind, 1)
    with socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_DGRAM if transport == 'udp' else socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.bind(('::1' if ipv6 else '127.0.0.2', 0))
        target = ('::1' if ipv6 else '127.0.0.1', port)
        if transport == 'udp':
            sock.sendto(wire, target)
            response = sock.recv(65535)
        else:
            sock.connect(target)
            # Exercise split TCP length prefix and message body.
            frame = struct.pack('!H', len(wire)) + wire
            sock.sendall(frame[:1]); sock.sendall(frame[1:7]); sock.sendall(frame[7:])
            def exact(size):
                result = bytearray()
                while len(result) < size:
                    data = sock.recv(size - len(result))
                    if not data: raise ValueError('short_tcp_reply')
                    result.extend(data)
                return bytes(result)
            response = exact(struct.unpack('!H', exact(2))[0])
    return {'rcode': response[3] & 15, 'answers': struct.unpack_from('!H', response, 6)[0]}


def wait(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result: return result
        except (OSError, ValueError, KeyError): pass
        time.sleep(.05)
    raise TimeoutError('isolated acceptance condition not reached')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--load-queries', type=int, default=400)
    parser.add_argument('--comparison-queries', type=int, default=80)
    args = parser.parse_args()
    if not 0 <= args.load_queries <= 2000: raise ValueError('bounded_load_required')
    if not 0 <= args.comparison_queries <= 200: raise ValueError('bounded_comparison_required')
    root = args.root.resolve()
    root.mkdir(parents=True, mode=0o700)  # Each run gets a new evidence directory.
    run, state = root / 'run', root / 'state'
    run.mkdir(); state.mkdir()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0)); port = reserve.getsockname()[1]
    config = {'log': {'level': 'error', 'file': ''}, 'plugins': [
        {'tag': 'hosts', 'type': 'hosts', 'args': {'entries': ['recording.test 192.0.2.8 2001:db8::8']}},
        {'tag': 'entry', 'type': 'sequence', 'args': [{'exec': '$hosts'}, {'matches': 'has_wanted_ans', 'exec': 'ok'}, {'exec': 'reject 3'}]},
        *[{'tag': f'{transport}{family}', 'type': transport + '_server', 'args': {'entry': 'entry', 'listen': f'{address}:{port}'}}
          for transport in ('udp', 'tcp') for family, address in [(4, '127.0.0.1'), (6, '[::1]')]]]}
    (root / 'mosdns.json').write_text(json.dumps(config))
    logs = []
    def child(command, name, env=None):
        log = (root / (name + '.log')).open('wb'); logs.append(log)
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
    core = child([str(args.home / 'core/mosdns'), 'start', '-c', str(root / 'mosdns.json')], 'mosdns')
    recorder = None
    observer = None
    results = {'scope': 'isolated loopback MosDNS and private recorder; no installed DNS reconfiguration',
               'version': subprocess.check_output([str(args.home / 'core/mosdns'), 'version'], text=True).strip(),
               'binary_sha256': hashlib.sha256((args.home / 'libexec/mosdns').read_bytes()).hexdigest(),
               'source_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / 'integrated/runtime').glob('query_*.py'))}}
    try:
        wait(lambda: query(port)['rcode'] == 0)
        ticks = str(process_start(Path('/proc') / str(core.pid)))
        endpoints = [{'address': address, 'port': port, 'transport': transport, 'family': family, 'wildcard': False}
                     for address, family in [('127.0.0.1', 4), ('::1', 6)] for transport in ('udp', 'tcp')]

        class Observer(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(1)
                if self.rfile.readline(32) != b'snapshot\n': return
                value = {'schema_version': 1, 'sampled_at': time.time(), 'dns': {'entry_component': 'mosdns', 'listeners': {
                    'mosdns': {'available': core.poll() is None, 'pid': core.pid, 'start_ticks': ticks, 'endpoints': endpoints}}}}
                self.wfile.write(json.dumps(value).encode() + b'\n')

        observer = socketserver.UnixStreamServer(str(run / 'observer.sock'), Observer)
        threading.Thread(target=observer.serve_forever, daemon=True).start()
        env = {**os.environ, 'SUITE_STATE_DIR': str(state), 'SUITE_RUN_DIR': str(run),
               'PYTHONPATH': str(ROOT / 'integrated'), 'PYTHONUNBUFFERED': '1'}
        def start():
            return child([sys.executable, '-m', 'runtime.query_recorder'], 'recorder', env)
        def request(operation='status', **kwargs):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3); sock.connect(str(run / 'query-recorder.sock'))
                sock.sendall(json.dumps({'operation': operation, **kwargs}).encode() + b'\n')
                with sock.makefile('rb') as stream: value = json.loads(stream.readline(512 * 1024))
            if not value['ok']: raise ValueError(value.get('error'))
            return value['result']
        recorder = start()
        assert wait(request)['enabled'] is False
        query(port); time.sleep(.3)
        assert request('recent')['events'] == []
        results['default_off'] = True
        request('configure', enabled=True)
        wait(lambda: request()['active'])
        cases = [('udp', 1, False, 'recording.test'), ('tcp', 28, False, 'recording.test'),
                 ('udp', 28, True, 'recording.test'), ('tcp', 1, True, 'recording.test'),
                 ('udp', 1, False, 'missing.test')]
        for index, (transport, kind, ipv6, domain) in enumerate(cases):
            answer = query(port, transport=transport, kind=kind, ipv6=ipv6, domain=domain, ident=100 + index)
            assert answer['rcode'] == (3 if domain == 'missing.test' else 0)
        events = wait(lambda: (value if len(value := request('recent')['events']) == len(cases) else None))
        assert {e['source_ip'] for e in events} == {'127.0.0.2', '::1'}
        assert {e['transport'] for e in events} == {'udp', 'tcp'}
        assert all(e['elapsed_ms'] is not None and e['branch'] is None and e['cache_hit'] is None for e in events)
        assert any(e['rcode'] == 'NXDOMAIN' for e in events)
        assert any('192.0.2.8' in e['addresses'] for e in events)
        assert any('2001:db8::8' in e['addresses'] for e in events)
        results['controlled_events'] = events
        if args.load_queries:
            def load(index):
                # Eight concurrent workers, each paced: bounded offered load.
                time.sleep(.01)
                return query(port, ident=1000 + index)['rcode']
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(load, range(args.load_queries)))
            assert all(code == 0 for code in responses)
            status = wait(lambda: (v if (v := request())['retained_rows'] >= len(cases) + args.load_queries else None))
            results['load'] = {'queries': len(responses), 'elapsed_seconds': time.monotonic() - started,
                               'recorder_status': status}
            assert not status['counters'].get('kernel_drops') and not status['counters'].get('queue_drops')
        retained = request()['retained_rows']
        if args.comparison_queries:
            def resources(pid):
                process = Path('/proc') / str(pid)
                fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
                values = {line.split(':')[0]: line.split(':')[1].strip() for line in (process / 'status').read_text().splitlines() if ':' in line}
                return {'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                        'rss_kib': int(values['VmRSS'].split()[0]), 'high_water_rss_kib': int(values['VmHWM'].split()[0])}
            rounds = []
            for round_index, enabled in enumerate([False, True, True, False]):
                request('configure', enabled=enabled)
                if enabled: wait(lambda: request()['active'])
                time.sleep(1.2)  # Exclude capture open/close transitions.
                before_core, before_recorder = resources(core.pid), resources(recorder.pid)
                count_before = request()['retained_rows']
                latencies = []
                for index in range(args.comparison_queries):
                    started = time.monotonic()
                    answer = query(port, ident=20000 + round_index * 1000 + index)
                    latencies.append((time.monotonic() - started) * 1000)
                    assert answer == {'rcode': 0, 'answers': 1}
                    time.sleep(max(0, .01 - (time.monotonic() - started)))
                if enabled:
                    wait(lambda: request()['retained_rows'] >= count_before + args.comparison_queries)
                else:
                    assert request()['retained_rows'] == count_before
                after_core, after_recorder = resources(core.pid), resources(recorder.pid)
                ordered = sorted(latencies)
                rounds.append({'enabled': enabled, 'queries': len(latencies), 'offered_qps_limit': 100,
                    'client_elapsed_ms': {str(p): ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] for p in [50, 95, 99]},
                    'core_cpu_seconds': after_core['cpu_seconds'] - before_core['cpu_seconds'],
                    'recorder_cpu_seconds': after_recorder['cpu_seconds'] - before_recorder['cpu_seconds'],
                    'recorder_rss_before_kib': before_recorder['rss_kib'], 'recorder_rss_after_kib': after_recorder['rss_kib'],
                    'recorder_lifetime_high_water_rss_kib': after_recorder['high_water_rss_kib']})
            results['short_comparison'] = {'scope': 'ABBA short controlled hosts responses, socket setup included; descriptive evidence, not a capacity or no-regression claim', 'rounds': rounds}
            retained = request()['retained_rows']
            request('configure', enabled=True)
            wait(lambda: request()['active'])
        recorder.terminate(); recorder.wait(timeout=5)
        assert query(port, transport='tcp')['rcode'] == 0
        results['dns_survived_recorder_exit'] = True
        recorder = start()
        wait(lambda: request()['active'])
        assert request()['retained_rows'] == retained
        results['enabled_and_rows_survived_restart'] = True
        request('configure', enabled=False)
        time.sleep(1.2); query(port); time.sleep(.3)
        assert request()['retained_rows'] == retained
        request('clear')
        assert request('recent')['events'] == []
        assert not request()['enabled']
        results['disable_and_clear'] = True
        results['final_status'] = request()
        results['passed'] = True
    finally:
        for process in (recorder, core):
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
        if observer: observer.shutdown(); observer.server_close()
        for log in logs: log.close()
        (root / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({'passed': results.get('passed', False), 'evidence': str(root / 'result.json'),
                      'controlled_events': len(results.get('controlled_events', [])), 'load_queries': args.load_queries}))


if __name__ == '__main__':
    main()
