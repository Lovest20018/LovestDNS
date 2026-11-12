"""Verify normal DNSCrypt UDP queues and paused-reader bursts, without timing claims."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import struct
import subprocess
import time

from dns_lab import Child, parse, question, read_name

ROOT = Path(__file__).resolve().parents[1]
source = runpy.run_path(str(ROOT / 'scripts/benchmark-dnscrypt-source.py'))
compat = runpy.run_path(str(ROOT / 'scripts/test-dnscrypt-compatibility.py'))
REQUEST = 256 * 1024
PACKETS = 128
CASES = ('baseline', 'upstream', 'buffer', 'udp', 'cache', 'combined')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def limit_evidence():
    names = ('rmem_max', 'rmem_default')
    paths = [Path('/proc/sys/net/core', name) for name in names]
    present = [path.exists() for path in paths]
    current = Path('/proc/self/ns/net').stat().st_ino
    reference = Path('/proc/1/ns/net').stat().st_ino
    if all(present):
        values = [path.read_text().strip() for path in paths]
        origin = 'current-namespace-procfs'
    elif any(present):
        raise ValueError('Incomplete kernel receive limit view')
    else:
        # Linux 6.1 omits these global settings from a non-init namespace.
        # Enter the reference namespace only in a short-lived, read-only cat;
        # DNS clients, fixtures and resolvers stay in the disposable namespace.
        if current == reference:
            raise ValueError('Missing receive limits in the reference namespace')
        values = subprocess.check_output(['nsenter', '--net=/proc/1/ns/net', '--', 'cat',
            *map(str, paths)], text=True, timeout=5).splitlines()
        origin = 'reference-namespace-readonly-cat'
    if len(values) != 2 or any(not re.fullmatch(r'\d+', value) or not 0 < int(value) < 2**31 for value in values):
        raise ValueError('Invalid kernel receive limits')
    return {'values': dict(zip(names, map(int, values))), 'source': origin,
            'namespace_inode': current, 'reference_namespace_inode': reference,
            'host_sysctl_writes': False}


def limits():
    return limit_evidence()['values']


def capability(pid):
    text = Path('/proc', str(pid), 'status').read_text()
    return bool(int(re.search(r'^CapEff:\s*([0-9a-f]+)', text, re.M)[1], 16) & (1 << 12))


def queue_snapshot(port, family):
    command = ['ss', '-u', '-a', '-n', '-m', '-H', '-4' if family == socket.AF_INET else '-6',
               'sport = :' + str(port)]
    output = subprocess.check_output(command, text=True, timeout=5)
    matches = re.findall(r'skmem:\(r(\d+),rb(\d+),t(\d+),tb(\d+),f(\d+),w(\d+),o(\d+),bl(\d+),d(\d+)\)', output)
    if len(matches) != 1:
        raise ValueError('Expected one UDP socket with kernel memory counters: ' + output)
    return dict(zip(('rx_bytes', 'receive_buffer', 'tx_bytes', 'send_buffer', 'forward', 'write', 'opt', 'backlog', 'drops'),
                    map(int, matches[0])))


def packet(name, kind, identifier, length=256):
    raw = question(name, kind, identifier)
    padding = length - len(raw) - 15
    if padding < 0 or length > 4095:
        raise ValueError('Requested packet cannot contain this question and an EDNS padding option')
    raw = raw[:10] + b'\0\1' + raw[12:]
    return raw + struct.pack('!BHHIH', 0, 41, 4096, 0, padding + 4) + struct.pack('!HH', 12, padding) + b'\0' * padding


def check_answer(raw, expected):
    answer = parse(raw)
    identifier = answer['id']
    if identifier not in expected:
        raise ValueError('Unexpected or duplicate DNS transaction')
    name, kind = expected[identifier]
    qname, end = read_name(raw, 12)
    if (struct.unpack('!H', raw[2:4])[0] & 0x8000 == 0 or raw[4:6] != b'\0\1'
            or qname.lower() != name or struct.unpack('!HH', raw[end:end+4]) != (kind, 1)
            or answer['rcode'] != 0 or answer['truncated'] or len(answer['answers']) != 1):
        raise ValueError('Wrong DNS response question or status')
    value = '192.0.2.10' if kind == 1 else '2001:db8::10'
    rr = answer['answers'][0]
    if rr['name'].lower() != name or rr['type'] != kind or rr['class'] != 1 or rr['ttl'] <= 0 or rr['value'] != value:
        raise ValueError('Wrong DNS answer or expired TTL')
    del expected[identifier]


def queue_checks(case, admin, kernel, before, queued, after, received, count=PACKETS):
    expected_buffer = 2 * min(REQUEST, kernel['rmem_max']) if case in ('buffer', 'udp', 'combined') else 8192 if admin else kernel['rmem_default']
    drops = queued['drops'] - before['drops']
    expected_loss = case in ('baseline', 'upstream', 'cache') and admin
    return {'buffer': before['receive_buffer'] == expected_buffer,
            'paused_queue': queued['rx_bytes'] > 0 and queued['receive_buffer'] == before['receive_buffer'],
            'drop_accounting': drops >= 0 and received + drops == count and after['drops'] == queued['drops'],
            'drained': after['rx_bytes'] == 0,
            'burst': (0 < received < count and drops > 0) if expected_loss else received == count and drops == 0}


def wait_stopped(child):
    deadline = time.monotonic() + 5
    child.process.send_signal(signal.SIGSTOP)
    while True:
        child.check()
        status = Path('/proc', str(child.process.pid), 'status').read_text()
        if re.search(r'^State:\s+T', status, re.M):
            return
        if time.monotonic() > deadline:
            raise TimeoutError('Isolated resolver did not stop')
        time.sleep(.01)


def exercise(child, address, family, case, admin, kernel, get_state):
    port = address[1]
    name = 'queue.cache.test'
    with socket.socket(family, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        client.settimeout(2)
        client.connect(address)
        deadline = time.monotonic() + 15
        while True:
            child.check()
            try:
                client.send(packet(name, 1, 65000))
                check_answer(client.recv(65535), {65000: (name, 1)})
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.05)
        client.send(packet(name, 28, 65001))
        check_answer(client.recv(65535), {65001: (name, 28)})
        warm = get_state()
        for identifier, kind, size in ((65002, 1, 4095), (65003, 28, 4095)):
            client.send(packet(name, kind, identifier, size))
            check_answer(client.recv(65535), {identifier: (name, kind)})
        before = queue_snapshot(port, family)
        client_before = queue_snapshot(client.getsockname()[1], family)
        expected = {identifier: (name, 28 if identifier % 5 == 0 else 1) for identifier in range(PACKETS)}
        wait_stopped(child)
        try:
            for identifier, (_, kind) in expected.items():
                client.send(packet(name, kind, identifier))
            queued = queue_snapshot(port, family)
        finally:
            child.process.send_signal(signal.SIGCONT)
        # All datagrams are queued before SIGCONT. A fixed, bounded drain
        # observes loss; its wall time is never interpreted as throughput.
        deadline = time.monotonic() + 8
        while expected and time.monotonic() < deadline:
            client.settimeout(min(.5, max(.001, deadline-time.monotonic())))
            try:
                check_answer(client.recv(65535), expected)
            except socket.timeout:
                if PACKETS - len(expected) + queued['drops'] - before['drops'] == PACKETS:
                    break
        after = queue_snapshot(port, family)
        result = {'before': before, 'paused': queued, 'after': after,
                  'sent': PACKETS, 'received': PACKETS-len(expected), 'missing_ids': sorted(expected),
                  'client_receive_buffer': client.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                  'max_length_queries': 2, 'client_before': client_before,
                  'client_after': queue_snapshot(client.getsockname()[1], family),
                  'measured_http': source['protocol_evidence'](warm, get_state(), 'h2')}
        result['checks'] = queue_checks(case, admin, kernel, before, queued, after, result['received'])
        result['checks']['client_drops'] = result['client_after']['drops'] == client_before['drops'] == 0
        measured = result['measured_http']
        result['checks']['cache_only'] = (measured['requests'] == measured['rejected'] == measured['dns_errors'] == 0
            and not any(measured[name] for name in ('routes','protocols','alpn')))
        return result


def controls():
    rows = []
    for family in (socket.AF_INET, socket.AF_INET6):
        for requested in (4096, 65536, REQUEST, 1024*1024):
            with socket.socket(family, socket.SOCK_DGRAM) as stream:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested)
                actual = stream.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                if actual != 2 * min(requested, limits()['rmem_max']):
                    raise ValueError('Kernel SO_RCVBUF clamping differs from the expected Linux rule')
                rows.append({'family': family.name, 'requested': requested, 'actual': actual})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'candidates', 'work'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--cache-candidate', type=Path)
    parser.add_argument('--cases', default=','.join(CASES[:-1]))
    args = parser.parse_args()
    cases = args.cases.split(',')
    if args.work.exists() or not cases or len(set(cases)) != len(cases) or set(cases)-set(CASES):
        parser.error('Use a fresh directory and distinct known candidates')
    if 'cache' in cases and args.cache_candidate is None:
        parser.error('--cache-candidate is required when selecting cache')
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Requires a disposable network namespace with IPv4/IPv6 loopback enabled')
    if not capability(os.getpid()):
        parser.error('NET_ADMIN is required to exercise both permission cases; no skipped coverage')
    binaries, builds = {}, {}
    for case in cases:
        binaries[case], builds[case] = (source['baseline_binary'](args.baseline_home) if case == 'baseline' else
            source['source_binary'](args.cache_candidate if case == 'cache' else args.candidates/case, case))
    fixture = args.candidates/'doh-fixture'
    manifest = json.loads((args.candidates/'fixture-manifest.json').read_text())
    if (manifest.get('fixture_only') is not True or digest(fixture) != manifest['binary_sha256']
            or digest(ROOT/'scripts/fixtures/doh/main.go') != manifest['source_sha256']):
        raise ValueError('DoH fixture identity mismatch')
    args.work.mkdir()
    work = args.work.resolve()
    kernel_source = limit_evidence()
    kernel = kernel_source['values']
    env = {'kernel': kernel, 'socket_controls': controls(), 'builds': builds, 'cases': cases,
           'kernel_limit_evidence': kernel_source, 'kernel_release': os.uname().release,
           'os_release': Path('/etc/os-release').read_text(),
           'fixture': manifest, 'method_sha256': digest(Path(__file__)), 'performance_acceptance': False,
           'limitations': ['One Linux kernel and its existing sysctl settings; no host sysctl writes.',
                           'Paused-reader 128x256-byte cache-hit burst is not a timing or maximum-throughput test.',
                           'Only explicitly selected candidates are covered; no default installation is approved.']}
    (work/'environment.json').write_text(json.dumps(env,indent=2)+'\n')
    cert, key = work/'cert.pem', work/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    key.chmod(0o600)
    rows = []
    with contextlib.ExitStack() as stack:
        fixture_child = Child([fixture,'-cert',cert,'-key',key,'-protocol','h2','-route','direct'], work/'fixture.log')
        stack.callback(fixture_child.close)
        deadline = time.monotonic()+10
        while not (work/'fixture.log').stat().st_size:
            fixture_child.check()
            if time.monotonic()>deadline: raise TimeoutError('DoH fixture readiness')
            time.sleep(.05)
        address = json.loads((work/'fixture.log').read_text().splitlines()[0])
        https_port = int(address['https'].rsplit(':',1)[1])
        for case in cases:
            for admin in (True, False):
                for family in (socket.AF_INET, socket.AF_INET6):
                    directory = work/f'{case}-admin{int(admin)}-{family.name}'
                    directory.mkdir()
                    host = '127.0.0.1' if family == socket.AF_INET else '::1'
                    with socket.socket(family, socket.SOCK_DGRAM) as reserve:
                        reserve.bind((host,0)); port = reserve.getsockname()[1]
                    listen = f'{host}:{port}' if family == socket.AF_INET else f'[{host}]:{port}'
                    config = directory/'dnscrypt.toml'
                    config.write_text(f'''server_names = ['fixture']
listen_addresses = ['{listen}']
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
cache_size = 512
cache_min_ttl = 0
cache_max_ttl = 600
''' + ("pqdnscrypt = false\nlb_strategy = 'p2'\n" if case != 'baseline' else '') +
                        f"[static.'fixture']\nstamp = '{compat['stamp'](https_port)}'\n")
                    before = source['state'](address['state'])
                    argv = ['env','GOMAXPROCS=2','SSL_CERT_FILE='+str(cert),binaries[case],'-config',config]
                    if not admin: argv = ['setpriv','--bounding-set=-net_admin',*argv]
                    child = Child(argv,directory/'dnscrypt.log')
                    try:
                        evidence = exercise(child,(host,port),family,case,admin,kernel,
                                            lambda: source['state'](address['state']))
                        observed_admin = capability(child.process.pid)
                        if observed_admin != admin: raise ValueError('Resolver capability mismatch')
                        after = source['state'](address['state'])
                        http = source['protocol_evidence'](before,after,'h2')
                        evidence['checks']['http2'] = http['valid'] and http['routes'] == {'direct':http['requests']}
                        row = {'case':case,'net_admin':observed_admin,'family':family.name,'http':http,
                               'lossless':evidence['received']==PACKETS,
                               'known_small_queue':admin and case in ('baseline','upstream','cache'),
                               **evidence,'passed':all(evidence['checks'].values()),'performance_acceptance':False}
                        rows.append(row)
                        (work/'results.json').write_text(json.dumps({'environment':env,'runs':rows,'release_approved':False},indent=2)+'\n')
                        print(json.dumps({name:row[name] for name in ('case','net_admin','family','received','checks','passed')}),flush=True)
                        child.check()
                        if not row['passed']: raise RuntimeError('Runtime UDP contract failed; retained evidence and stopped')
                    finally:
                        if child.process.poll() is None: child.process.send_signal(signal.SIGCONT)
                        child.close()
    if limits() != kernel: raise ValueError('Kernel receive limits changed during the matrix')


if __name__ == '__main__':
    main()
