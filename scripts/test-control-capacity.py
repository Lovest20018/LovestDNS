"""Bounded slow-DNS load on a marked native test instance; never touch host DNS."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import socket
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--duration', type=int, default=180)
    parser.add_argument('--allow-saturation', action='store_true', help='Record the previous implementation without asserting latency')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.duration <= 600 or not args.instance.isascii() or not args.instance.replace('-', '').isalnum():
        parser.error('Named instance and duration 10..600 required')
    home = Path('/opt/lovestdns-' + args.instance) / 'current'
    spec = importlib.util.spec_from_file_location('installer', home / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    p = installer.paths(args.instance)
    if not (p['storage'] / 'data/.lovestdns-test-instance').is_file():
        parser.error('Refusing instance without the test marker')
    config = installer.read_env(p['config'])
    auth = [str(home/'venv/bin/python'), str(home/'app/backend/browser_auth.py'), '--state-dir', str(p['storage']/'state'), '--external-api']
    def control(*options):
        return subprocess.check_output([*auth, *options], text=True, timeout=15).strip()
    originally_on = control('status').endswith('enabled')
    host = '::1' if config['WEB_BIND_ADDRESS'] == '::' else '127.0.0.1' if config['WEB_BIND_ADDRESS'] == '0.0.0.0' else config['WEB_BIND_ADDRESS']
    base = 'http://' + ('['+host+']' if ':' in host else host) + ':' + config['WEB_HOST_PORT'] + '/api/v1/'
    def request(route, body=None):
        # A separate opener per thread avoids shared HTTP client state.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(base+route, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+config['WEB_UI_TOKEN'], 'Content-Type':'application/json'})
        started = time.monotonic()
        try:
            with opener.open(req, timeout=15) as response:
                return response.status, json.load(response), time.monotonic()-started
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc), time.monotonic()-started
    group = subprocess.check_output(['systemctl','show',p['name']+'.service','--property=ControlGroup','--value'],text=True).strip()
    cgroup = Path('/sys/fs/cgroup') / group.lstrip('/')
    def cpu():
        return int(dict(line.split() for line in (cgroup/'cpu.stat').read_text().splitlines())['usage_usec'])
    def memory():
        rss = 0
        for value in (cgroup/'cgroup.procs').read_text().splitlines():
            try:
                for line in Path('/proc',value,'status').read_text().splitlines():
                    if line.startswith('VmRSS:'):rss += int(line.split()[1])*1024
            except FileNotFoundError:pass
        return {'cgroup_bytes':int((cgroup/'memory.current').read_text()),'sum_process_rss_bytes':rss}
    blackhole = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    blackhole.bind(('127.0.0.1',0));blackhole.settimeout(.2)
    stop = threading.Event()
    def discard():
        while not stop.is_set():
            try:blackhole.recvfrom(4096)
            except socket.timeout:pass
    receiver = threading.Thread(target=discard,daemon=True)
    receiver.start()
    payload = {'domain':'capacity-test.invalid','record_type':'A','targets':[
        {'label':'slow-'+str(i),'server':'127.0.0.1','port':blackhole.getsockname()[1],'transport':'udp'} for i in range(4)]}
    flood = []
    def slow_queries():
        while not stop.is_set():
            try:
                status, body, elapsed = request('dns-compare', payload)
                if status == 503:
                    assert body['error']['code'] == 'UNAVAILABLE'
                else:
                    assert status == 200 and len(body['data']['tests']) == 4
                flood.append((status,elapsed))
            except Exception as exc:
                flood.append(('error',str(exc)))
            stop.wait(.15)
    reads, failures, memory_samples = [], [], []
    report = {'duration_requested_seconds':args.duration,'flood_clients':4,'targets_per_comparison':4,
              'mode':'baseline' if args.allow_saturation else 'bounded',
              'version':json.loads((home/'manifest.json').read_text())['version']}
    try:
        control('on')
        before_cpu, started = cpu(), time.monotonic()
        with ThreadPoolExecutor(4) as pool:
            jobs = [pool.submit(slow_queries) for _ in range(4)]
            try:
                stop.wait(.5)
                while time.monotonic()-started < args.duration:
                    for route in ('instance','runtime','maintenance/tasks'):
                        status,_,elapsed = request(route)
                        reads.append(elapsed)
                        if status != 200:failures.append({'route':route,'status':status})
                    memory_samples.append(memory())
                    stop.wait(.2)
            finally:
                stop.set()
            for job in jobs:job.result(timeout=20)
        elapsed = time.monotonic()-started
        ordered = sorted(reads)
        report.update(wall_seconds=round(elapsed,3), read_requests=len(reads), read_failures=failures,
            read_p50_seconds=round(statistics.median(reads),4),read_p95_seconds=round(ordered[max(0,int(len(reads)*.95)-1)],4),
            read_max_seconds=round(max(reads),4), comparisons_completed=sum(x[0]==200 for x in flood),
            comparisons_busy=sum(x[0]==503 for x in flood),comparison_errors=[x[1] for x in flood if x[0]=='error'],
            cpu_seconds=round((cpu()-before_cpu)/1_000_000,3),
            peak_cgroup_bytes=max(x['cgroup_bytes'] for x in memory_samples),
            peak_sum_process_rss_bytes=max(x['sum_process_rss_bytes'] for x in memory_samples))
        report['cpu_percent_one_core'] = round(report['cpu_seconds']/elapsed*100,2)
        if not args.allow_saturation:
            assert not failures and not report['comparison_errors'],report
            assert report['comparisons_busy'] > 0 and report['comparisons_completed'] > 0,report
            assert report['read_p95_seconds'] < .5 and report['read_max_seconds'] < 2,report
    finally:
        stop.set();receiver.join(timeout=2);blackhole.close()
        control('on' if originally_on else 'off')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
