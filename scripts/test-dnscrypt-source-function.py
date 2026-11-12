"""Finite cache, protocol and controlled split-path checks for normal source candidates."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
from pathlib import Path
import runpy
import subprocess
import threading
import time

from dns_lab import CacheLab, Child, Fixture, free_port, query

ROOT = Path(__file__).resolve().parents[1]
source = runpy.run_path(str(ROOT/'scripts/benchmark-dnscrypt-source.py'))
compat = runpy.run_path(str(ROOT/'scripts/test-dnscrypt-compatibility.py'))
chain = runpy.run_path(str(ROOT/'scripts/benchmark-dnscrypt-chain.py'))


def checked(port, name, kind, ttl=600):
    response = query(port, name, qtype=kind, timeout=3)
    answers = response['answers']
    if response['rcode'] != 0 or response['truncated'] or len(answers) != 1:
        raise ValueError('Unexpected DNS answer status')
    answer = answers[0]
    expected = '192.0.2.10' if kind == 1 else '2001:db8::10'
    if (answer['name'].lower().rstrip('.') != name.lower().rstrip('.') or answer['class'] != 1
            or answer['type'] != kind or answer['value'] != expected or not 0 < answer['ttl'] <= ttl):
        raise ValueError('Wrong DNS answer, question type, address or TTL')
    return {'name': name, 'type': kind, 'ttl': answer['ttl'], 'id': response['id'], 'valid': True}


def question(index):
    # Each name has both types, so type isolation is exercised in the binary.
    return f'pool-{index//2}.cache.test', 28 if index % 2 else 1


def config_text(port, https_port, proxy_port, capacity, ttl):
    return f'''server_names = ['fixture']
listen_addresses = ['127.0.0.1:{port}']
max_clients = 250
ipv4_servers = true
ipv6_servers = false
dnscrypt_servers = false
doh_servers = true
require_dnssec = false
require_nolog = false
require_nofilter = false
http3 = false
ignore_system_dns = true
netprobe_address = '127.0.0.1:{https_port}'
netprobe_timeout = 3
cache = true
cache_size = {capacity}
cache_min_ttl = 0
cache_max_ttl = {ttl}
pqdnscrypt = false
lb_strategy = 'p2'
''' + (f"proxy = 'socks5://127.0.0.1:{proxy_port}'\n" if proxy_port else '') + f"[static.'fixture']\nstamp = '{compat['stamp'](https_port)}'\n"


@contextlib.contextmanager
def resolver(directory, binary, cert, https_port, proxy_port, capacity=512, ttl=600):
    port = free_port()
    config = directory/'dnscrypt.toml'
    config.write_text(config_text(port, https_port, proxy_port, capacity, ttl))
    child = Child(['setpriv','--bounding-set=-net_admin','env','GOMAXPROCS=2','SSL_CERT_FILE='+str(cert),
                   binary,'-config',config],directory/'dnscrypt.log')
    try:
        chain['ready'](child,port)
        yield child, port
        child.check()
    finally:
        child.close()


def cache_case(target, binary, cert, https_port, proxy_port, address, capacity, ttl):
    with resolver(target,binary,cert,https_port,proxy_port,capacity,ttl) as (_,port):
        before = source['state'](address['state'])
        requests, checks, phases = before['requests'], [], []
        if ttl == 2:
            for step in range(3):
                if step == 2: time.sleep(2.2)
                answer = checked(port,'expiry.cache.test',1,ttl)
                current = source['state'](address['state'])['requests']
                if current-requests != (0 if step == 1 else 1):
                    raise ValueError('Short TTL cache hit/expiry did not follow observed upstream counts')
                checks.append({**answer,'step':step,'upstream_delta':current-requests})
                requests = current
        else:
            for index in range(96):
                name, kind = question(index)
                for repeat in range(2):
                    answer = checked(port,name.upper() if repeat else name,kind)
                    current = source['state'](address['state'])['requests']
                    if current-requests != (0 if repeat else 1):
                        raise ValueError('Cold key/type isolation or immediate case-insensitive hit failed')
                    checks.append({**answer,'phase':'immediate-hit' if repeat else 'cold','upstream_delta':current-requests})
                    requests = current
            start = requests
            for index in range(96):
                name, kind = question(index)
                answer = checked(port,name,kind)
                current = source['state'](address['state'])['requests']
                if current-requests not in (0,1) or capacity == 512 and current != requests:
                    raise ValueError('Cache traversal upstream count or large-capacity retention failed')
                checks.append({**answer,'phase':'sweep','upstream_delta':current-requests})
                requests = current
            phases.append({'phase':'sweep','queries':96,'upstream':requests-start,'cache_hits':96-(requests-start)})

            def worker(identifier):
                values = []
                for step in range(32):
                    name, kind = ((f'cold-{identifier}-{step}.cache.test',28 if identifier%2 else 1)
                                  if step%4 == 0 else question((identifier*17+step)%96))
                    values.append({**checked(port,name,kind),'phase':'parallel','worker':identifier})
                return values

            start = requests
            with ThreadPoolExecutor(max_workers=8) as pool:
                for values in pool.map(worker,range(8)): checks.extend(values)
            requests = source['state'](address['state'])['requests']
            if not 64 <= requests-start <= 256 or capacity == 512 and requests-start != 64:
                raise ValueError('Concurrent cache upstream count or retained hot keys failed')
            phases.append({'phase':'parallel','queries':256,'upstream':requests-start,'cache_hits':256-(requests-start)})
        after = source['state'](address['state'])
        return {'kind':'cache','capacity':capacity,'ttl_max':ttl,'queries':len(checks),'checks':checks,'phases':phases}, before, after


def split_case(target, binary, cert, https_port, proxy_port, address, home):
    local = Fixture(ttl=600,threaded_udp=False,track_questions=True)
    with contextlib.ExitStack() as stack:
        stack.callback(local.close)
        lab = CacheLab(target/'local',[home/'core/redis-server'],[home/'core/redis-cli'],[home/'core/unbound'],[local])
        stack.callback(lab.close)
        _, port = stack.enter_context(resolver(target,binary,cert,https_port,proxy_port))
        mos_port = free_port()
        config = target/'mosdns.json'
        config.write_text(json.dumps(chain['mosdns_config'](mos_port,lab.ports[0],port)))
        mosdns = Child(['env','GOMAXPROCS=2',home/'core/mosdns','start','-c',config],target/'mosdns.log')
        stack.callback(mosdns.close)
        chain['ready'](mosdns,mos_port)
        before = source['state'](address['state'])
        rows = chain['question_rows']('split-cold',64)
        phases = []
        checks = []
        for scenario in ('split-cold','split-hot'):
            local_before, http_before, redis_before = local.question_snapshot(), source['state'](address['state']), chain['redis_counters'](lab)
            for name, kind in rows:
                checks.append(checked(mos_port,name.upper() if scenario == 'split-hot' else name,28 if kind == 'AAAA' else 1))
            after = source['state'](address['state'])
            evidence = chain['path_evidence'](scenario,rows,local_before,local.question_snapshot(),http_before,after,
                                              redis_before,chain['redis_counters'](lab),'h2')
            if not evidence['valid']: raise ValueError('Controlled split cache/forward path failed: '+json.dumps(evidence['checks']))
            phases.append({'phase':scenario,**evidence})
        for child in (mosdns,lab.redis,*lab.unbounds): child.check()
        return {'kind':'split','queries':len(checks),'checks':checks,'phases':phases}, before, after


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidates','baseline-home','work'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--host-netns-path',type=Path,default=Path('/proc/1/ns/net'),
                        help='Host network namespace reference; bind it read-only when running inside Docker')
    parser.add_argument('--cases',default='upstream,combined')
    args = parser.parse_args()
    cases = args.cases.split(',')
    allowed = json.loads(source['LOCK'].read_text())['variants']
    if args.work.exists() or not cases or len(set(cases)) != len(cases) or set(cases)-allowed.keys():
        parser.error('Use a fresh directory and distinct locked source candidates')
    if Path('/proc/self/ns/net').stat().st_ino == args.host_netns_path.stat().st_ino:
        parser.error('Requires a disposable network namespace with loopback enabled')
    builds, binaries = {}, {}
    for case in cases: binaries[case], builds[case] = source['source_binary'](args.candidates/case,case)
    home = args.baseline_home.resolve()
    identity = chain['runtime_identity'](home)
    fixture = args.candidates/'doh-fixture'
    manifest = json.loads((args.candidates/'fixture-manifest.json').read_text())
    if (manifest.get('fixture_only') is not True or source['digest'](fixture) != manifest['binary_sha256']
            or source['digest'](ROOT/'scripts/fixtures/doh/main.go') != manifest['source_sha256']):
        raise ValueError('DoH fixture identity mismatch')
    args.work.mkdir()
    work = args.work.resolve()
    environment = {'cases':cases,'builds':builds,'fixture':manifest,'runtime_sha256':identity,
        'network_namespace':{'self_inode':Path('/proc/self/ns/net').stat().st_ino,
                             'host_inode':args.host_netns_path.stat().st_ino},
        'method_sha256':source['digest'](Path(__file__)),'functional_only':True,'performance_acceptance':False,
        'limitations':['Controlled local DoH, caches and domain split; not production geo/fallback rules.',
                       'No throughput, process CPU or latency acceptance is performed.']}
    (work/'environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    cert, key = work/'cert.pem', work/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    key.chmod(0o600)
    outcomes = []
    for protocol in ('h1','h2'):
        for proxy in (False,True):
            with contextlib.ExitStack() as stack:
                log = work/f'fixture-{protocol}-{int(proxy)}.log'
                server = Child([fixture,'-cert',cert,'-key',key,'-protocol',protocol,'-route','proxy' if proxy else 'direct'],log)
                stack.callback(server.close)
                deadline = time.monotonic()+10
                while not log.stat().st_size:
                    server.check()
                    if time.monotonic()>deadline: raise TimeoutError('DoH fixture readiness')
                    time.sleep(.05)
                address = json.loads(log.read_text().splitlines()[0])
                https_port = int(address['https'].rsplit(':',1)[1])
                socks = compat['Socks'](('127.0.0.1',0),compat['SocksHandler'])
                socks.allowed_port, socks.connections, socks.tcp_nodelay = https_port, 0, True
                threading.Thread(target=socks.serve_forever,daemon=True).start()
                stack.callback(socks.server_close)
                stack.callback(socks.shutdown)
                proxy_port = socks.server_address[1] if proxy else None
                entries = [(case,'cache',capacity,600) for capacity in (1,17,512) for case in cases]
                if protocol == 'h2':
                    entries += [(case,'split',512,600) for case in cases]
                    if not proxy: entries += [(case,'cache',512,2) for case in cases]
                for case, kind, capacity, ttl in entries:
                    target = work/f'{protocol}-{int(proxy)}-{case}-{kind}-c{capacity}-ttl{ttl}'
                    target.mkdir()
                    initial, connections = source['state'](address['state']), socks.connections
                    if kind == 'cache':
                        result, before, after = cache_case(target,binaries[case],cert,https_port,proxy_port,address,capacity,ttl)
                    else:
                        result, before, after = split_case(target,binaries[case],cert,https_port,proxy_port,address,home)
                    http = source['protocol_evidence'](before,after,protocol)
                    if not http['valid']: raise ValueError('Unexpected HTTP protocol, ALPN or upstream error')
                    route = compat['route_evidence'](proxy,Counter(initial['routes']),Counter(after['routes']),connections,socks.connections)
                    row = {**result,'case':case,'protocol':protocol,'socks':proxy,'protocol_validation':http,
                           'route_validation':route,'passed':True,'functional_only':True,'performance_acceptance':False}
                    (target/'result.json').write_text(json.dumps(row,indent=2)+'\n')
                    outcomes.append(row)
                    (work/'results.json').write_text(json.dumps({'environment':environment,'runs':outcomes,'release_approved':False},indent=2)+'\n')
                    print(json.dumps({k:row[k] for k in ('case','kind','protocol','socks','queries','passed')}),flush=True)
    if chain['runtime_identity'](home) != identity: raise ValueError('Frozen baseline runtime changed')


if __name__ == '__main__':
    main()
