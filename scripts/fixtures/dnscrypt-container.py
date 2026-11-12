"""Serve only a disposable container's loopback DoH and actual SOCKS fixture."""
import argparse
import json
from pathlib import Path
import runpy
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from dns_lab import Child
compat = runpy.run_path(str(ROOT/'scripts/test-dnscrypt-compatibility.py'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    args = parser.parse_args()
    args.work.mkdir(exist_ok=True)
    log = args.work/'doh.log'
    child = Child([args.fixtures/'doh-fixture','-cert',args.work/'cert.pem','-key',args.work/'key.pem',
                   '-protocol','h2','-route','both'],log)
    socks = None
    def stop(*_): raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    try:
        deadline = time.monotonic()+10
        while not log.stat().st_size:
            child.check()
            if time.monotonic()>deadline: raise TimeoutError('Container DoH fixture startup')
            time.sleep(.05)
        address = json.loads(log.read_text().splitlines()[0])
        port = int(address['https'].rsplit(':',1)[1])
        socks = compat['Socks'](('127.0.0.1',1080),compat['SocksHandler'])
        socks.allowed_port,socks.connections,socks.tcp_nodelay = port,0,True
        threading.Thread(target=socks.serve_forever,daemon=True).start()
        template = f'''#socksokproxy = 'socks5://{{SOCKS5}}'
server_names = ['fixture']
listen_addresses = ['0.0.0.0:5302']
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
netprobe_address = '127.0.0.1:{port}'
netprobe_timeout = 3
cache = true
cache_size = 512
cache_min_ttl = 0
cache_max_ttl = 600
[static.'fixture']
stamp = '{compat['stamp'](port)}'
'''
        (args.work/'dnscrypt.toml').write_text(template)
        (args.work/'fixture.json').write_text(json.dumps({**address,'state_url':'http://'+address['state']+'/state',
            'network_namespace_inode':Path('/proc/self/ns/net').stat().st_ino,'fixture_only':True},indent=2)+'\n')
        print('Container loopback fixtures ready',flush=True)
        while True:
            child.check()
            time.sleep(1)
    finally:
        if socks: socks.shutdown();socks.server_close()
        child.close()


if __name__ == '__main__': main()
