"""Install, upgrade and recover a new marked native instance in an existing isolated netns."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

from dns_lab import Child, Fixture, query

ROOT = Path(__file__).resolve().parents[1]
native = runpy.run_path(str(ROOT/'packaging/native/install.py'))
source = runpy.run_path(str(ROOT/'scripts/benchmark-dnscrypt-source.py'))
compat = runpy.run_path(str(ROOT/'scripts/test-dnscrypt-compatibility.py'))
functions = runpy.run_path(str(ROOT/'scripts/test-dnscrypt-source-function.py'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def command(args, *, log=None, expected=0, timeout=360):
    result = subprocess.run(list(map(str,args)),text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    if log: log.write_text(result.stdout)
    if result.returncode != expected:
        raise RuntimeError(f'Command exit {result.returncode}, expected {expected}; see {log or args[0]}')
    return result.stdout.strip()


def resource_limits(cpus, quota, memory, available):
    if not re.fullmatch(r'\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*', cpus):
        raise ValueError('Use a comma-separated CPU list or ranges')
    selected = set()
    for item in cpus.split(','):
        bounds = list(map(int, item.split('-')))
        first, last = bounds[0], bounds[-1]
        if first > last or last - first > 4096:
            raise ValueError('Invalid CPU range')
        selected.update(range(first, last + 1))
    if not selected or not selected <= set(available):
        raise ValueError('Trial CPU list is outside the available affinity')
    if not 1 <= quota <= 300 or not 256 <= memory <= 2048:
        raise ValueError('Trial quota or memory limit is outside the supported range')
    return {'allowed_cpus': cpus, 'cpu_quota_percent': quota, 'memory_max_mb': memory}


def extract_package(archive_path, target):
    """Extract regular files without version-specific tar filters or metadata."""
    if not target.is_dir() or any(target.iterdir()):
        raise ValueError('Trial extraction requires a fresh empty directory')
    with tarfile.open(archive_path) as archive:
        entries, roots = {}, set()
        for entry in archive.getmembers():
            name = PurePosixPath(entry.name)
            path = target.joinpath(*name.parts)
            if (name.is_absolute() or not name.parts or '..' in name.parts
                    or '\\' in entry.name or ':' in entry.name
                    or not (entry.isfile() or entry.isdir()) or name in entries
                    or target.resolve() not in path.resolve().parents):
                raise ValueError('Unsafe or duplicate trial archive entry')
            entries[name] = entry
            roots.add(name.parts[0])
        if len(roots) != 1 or any(parent in entries and not entries[parent].isdir()
                for name in entries for parent in name.parents if parent.parts):
            raise ValueError('Expected one non-conflicting trial package root')
        # No links, device nodes, ownership, ACLs, or setuid/setgid bits are
        # restored. Explicit writes also work with Debian's early Python 3.11.
        for name, entry in entries.items():
            path = target.joinpath(*name.parts)
            if entry.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as source_file, path.open('xb') as destination:
                    shutil.copyfileobj(source_file, destination)
                path.chmod(entry.mode & 0o777)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('packages','fixtures','work','netns-path'): parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--allowed-cpus',default='4-7')
    parser.add_argument('--cpu-quota-percent',type=int,default=300)
    parser.add_argument('--memory-max-mb',type=int,default=768)
    args = parser.parse_args()
    try:
        limits = resource_limits(args.allowed_cpus,args.cpu_quota_percent,args.memory_max_mb,os.sched_getaffinity(0))
    except ValueError as error:
        parser.error(str(error))
    if os.geteuid() != 0 or not re.fullmatch(r'dnstrial-[a-z0-9-]{1,20}',args.instance):
        parser.error('Requires root and a new dnstrial-* instance')
    namespace = Path('/proc/self/ns/net').stat().st_ino
    if namespace == Path('/proc/1/ns/net').stat().st_ino or namespace != args.netns_path.stat().st_ino:
        parser.error('Run inside the supplied isolated network namespace')
    p = native['paths'](args.instance)
    drop = p['unit'].with_name(p['unit'].name+'.d')
    if args.work.exists() or any(q.exists() or q.is_symlink() for q in (*[p[k] for k in ('home','storage','config','unit','ctl','run')],drop)):
        parser.error('Instance and work paths must be fresh')
    for name in ('python3-venv','inotify-tools','iproute2','procps','xz-utils','ca-certificates','bash'):
        if command(['dpkg-query','-W','-f=${Status}',name]) != 'install ok installed':
            parser.error('Install required dependencies before this trial; no apt changes allowed')
    packages = json.loads((args.packages/'trial-packages.json').read_text())
    if not packages['candidate_only'] or packages['release_approved']: raise ValueError('Not a trial package set')
    for label,row in packages['packages'].items():
        path = args.packages/row['archive']
        if path.parent != args.packages or digest(path) != row['sha256']: raise ValueError('Trial archive identity mismatch')
    fixture = args.fixtures/'doh-fixture'
    fixture_manifest = json.loads((args.fixtures/'fixture-manifest.json').read_text())
    if (not fixture_manifest['fixture_only'] or digest(fixture) != fixture_manifest['binary_sha256']
            or digest(ROOT/'scripts/fixtures/doh/main.go') != fixture_manifest['source_sha256']):
        raise ValueError('Fixture identity mismatch')
    args.work.mkdir(parents=True)
    work = args.work.resolve()
    result = {'instance':args.instance,'packages':packages,'method_sha256':digest(Path(__file__)),
        'fixture':fixture_manifest,'stages':[],'passed':False,'functional_only':True,
        'environment':{'os_release':Path('/etc/os-release').read_text(),'kernel':os.uname().release,
            'python':sys.version,'systemd':command(['systemctl','--version']).splitlines()[0],
            'resources':limits,'network_namespace_inode':namespace},
        'performance_acceptance':False,'release_approved':False}
    def record(stage, **values):
        result['stages'].append({'stage':stage,**values})
        (work/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'stage':stage,**values}),flush=True)
    def stop_on_signal(*_): raise KeyboardInterrupt('Native trial interrupted')
    signal.signal(signal.SIGTERM,stop_on_signal)
    cert,key = work/'cert.pem',work/'key.pem'
    command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,
        '-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],log=work/'certificate.log')
    key.chmod(0o600)
    control = packages['packages']['control']
    unpack = work/'control';unpack.mkdir()
    extract_package(args.packages/control['archive'],unpack)
    package = next(unpack.iterdir())
    native['verify_package'](package)
    cert_info = (f'[Service]\nNetworkNamespacePath={args.netns_path}\nAllowedCPUs={args.allowed_cpus}\n'
        f'CPUQuota={args.cpu_quota_percent}%\nMemoryMax={args.memory_max_mb}M\nNice=10\n'
        f'IOSchedulingClass=best-effort\nIOSchedulingPriority=7\nEnvironment=GOMAXPROCS=2\nEnvironment=SSL_CERT_FILE={cert}\n')
    drop.mkdir(parents=True);(drop/'trial.conf').write_text(cert_info)
    installer_path = lambda: p['home']/'current/install.py'
    unit = p['name']+'.service'
    service = lambda: dict(line.split('=',1) for line in command(['systemctl','show',unit,'-p','MainPID','-p','NRestarts','-p','ActiveState']).splitlines())
    def install_command(action, *values, expected=0):
        return command([sys.executable,installer_path(),'--instance',args.instance,action,*values],
            log=work/(action+'-'+str(len(result['stages']))+'.log'),expected=expected)
    def restart():
        install_command('restart')
    def redis(*values, db=0):
        pid = service()['MainPID']
        return command(['nsenter','-t',pid,'-m','--',p['home']/'current/core/redis-cli','-s','/tmp/redis.sock','-n',db,*values])
    def redis_stats():
        values = dict(line.split(':',1) for line in redis('INFO','stats').splitlines() if ':' in line)
        return {k:int(values[k]) for k in ('keyspace_hits','keyspace_misses')}
    def cfg_hashes():
        return {n:digest(p['storage']/'data'/n) for n in ('custom_env.ini','custom_mod.yaml','unbound_custom.conf','dnscrypt.toml')}
    def app_probe():
        home=p['home']/'current'
        command([home/'venv/bin/python',home/'app/backend/browser_auth.py','--state-dir',p['storage']/'state','--external-api','on'])
        config=native['read_env'](p['config'])
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path, observer=True):
            request=urllib.request.Request('http://127.0.0.1:18123'+path,
                headers={'Authorization':'Bearer '+config['SUITE_READ_TOKEN' if observer else 'WEB_UI_TOKEN']})
            with opener.open(request,timeout=15) as response:return json.load(response)
        runtime=get('/api/v1/runtime')['data'];caps=get('/api/v1/capabilities')['data']
        if not runtime['core_healthy'] or caps['deployment'] != 'native':raise ValueError('Native management API failed')
        for operation in ('recursive','forward'):
            if not runtime['layers'][operation]['available']:raise ValueError('Unbound layer unavailable')
        return get('/api/v1/instance')['data']['id']
    questions=[(f'{"MiXeD" if i%2 else "lower"}-{i}.cache.test',kind) for i in range(12) for kind in (1,28)]
    def cachedb(stage, fixtures, *, retained, question_set=None):
        rows=questions if question_set is None else question_set
        before=redis_stats();counts=[f.requests() for f in fixtures];ttls=[]
        for db,fixture in enumerate(fixtures):
            for name,kind in rows:
                answer=query(5301 if db==0 else 5304,name,qtype=kind,timeout=3)
                expected=fixture.address if kind==1 else '2001:db8::10'
                if (answer['rcode']!=0 or len(answer['answers'])!=1 or answer['answers'][0]['type']!=kind
                        or answer['answers'][0]['value']!=expected or not 0<answer['answers'][0]['ttl']<=3600):
                    raise ValueError('Retained cachedb answer or database isolation failed')
                ttls.append(answer['answers'][0]['ttl'])
        after=redis_stats();delta={k:after[k]-before[k] for k in before};up=[f.requests()-v for f,v in zip(fixtures,counts)]
        count=2*len(rows)
        if retained and (delta!={'keyspace_hits':count,'keyspace_misses':0} or up!=[0,0]):
            raise ValueError('Expected first native queries to reuse persisted Redis entries: '+json.dumps({'redis':delta,'upstream':up}))
        if not retained and (delta!={'keyspace_hits':0,'keyspace_misses':count} or up!=[len(rows),len(rows)]):
            raise ValueError('Cold native cache population did not follow both DB paths')
        if [redis('GET','trial-marker',db=db) for db in (0,1)]!=['original-db0','original-db1']:
            raise ValueError('Redis user markers crossed databases or were lost')
        record(stage,queries=count,redis_delta=delta,upstream_by_db=up,min_ttl=min(ttls),db_markers_preserved=True)
    def protocol(stage, address, socks):
        before=source['state'](address['state']);connections=socks.connections
        for port in (5302,5303):
            for i in range(4):
                name=f'{stage}-{port}-{i}.enc.cache.test'
                for transport in (False,True):
                    r=query(port,name,qtype=28 if i%2 else 1,tcp=transport,timeout=4)
                    expected='2001:db8::10' if i%2 else '192.0.2.10'
                    if r['rcode'] or not r['answers'] or r['answers'][0]['value']!=expected:raise ValueError('Native DNSCrypt UDP/TCP answer failed')
        after=source['state'](address['state']);evidence=source['protocol_evidence'](before,after,'h2')
        routes={k:after['routes'].get(k,0)-before['routes'].get(k,0) for k in ('direct','proxy')}
        if not evidence['valid'] or routes!={'direct':4,'proxy':4}:raise ValueError('Native HTTP/ALPN/direct/SOCKS evidence failed')
        # TCP and UDP queries share the resolver cache; connection reuse may
        # mean no new SOCKS dial in this phase, so verify the actual source too.
        record(stage,queries=16,http=evidence,routes=routes,socks_connections=socks.connections-connections)
    def split(stage, fixtures, address):
        before=[f.requests() for f in fixtures];http=source['state'](address['state'])
        rows=[(f'{stage}-{i}.{"local" if i%2==0 else "enc"}.cache.test',28 if i%3==0 else 1) for i in range(16)]
        for name,kind in rows:functions['checked'](15353,name,kind,3600)
        after=[f.requests() for f in fixtures];current=source['state'](address['state'])
        if [a-b for a,b in zip(after,before)]!=[8,0] or current['requests']-http['requests']!=8:
            raise ValueError('Native MosDNS cold split did not reach the expected backends')
        before_redis=redis_stats()
        for name,kind in rows:functions['checked'](15353,name.upper(),kind,3600)
        if after!=[f.requests() for f in fixtures] or source['state'](address['state'])['requests']!=current['requests'] or redis_stats()!=before_redis:
            raise ValueError('Native MosDNS hot cache did not block backends')
        record(stage,queries=32,cold_local=8,cold_doh=8,hot_backend_requests=0)
    started=False
    with contextlib.ExitStack() as stack:
        try:
            fixtures=[Fixture('192.0.2.10',ttl=3600,threaded_udp=False,track_questions=True),
                      Fixture('192.0.2.20',ttl=3600,threaded_udp=False,track_questions=True)]
            for f in fixtures:stack.callback(f.close)
            log=work/'fixture.log'
            server=Child([fixture,'-cert',cert,'-key',key,'-protocol','h2','-route','both'],log);stack.callback(server.close)
            deadline=time.monotonic()+10
            while not log.stat().st_size:
                server.check()
                if time.monotonic()>deadline:raise TimeoutError('Native DoH fixture startup')
                time.sleep(.05)
            address=json.loads(log.read_text().splitlines()[0]);https_port=int(address['https'].rsplit(':',1)[1])
            socks=compat['Socks'](('127.0.0.1',0),compat['SocksHandler'])
            socks.allowed_port,socks.connections,socks.tcp_nodelay=https_port,0,True
            threading.Thread(target=socks.serve_forever,daemon=True).start()
            stack.callback(socks.server_close);stack.callback(socks.shutdown)
            seed=work/'seed';seed.mkdir()
            (seed/'.lovestdns-test-instance').write_text('synthetic native DNSCrypt trial\n')
            (seed/'custom_env.ini').write_text('TRIAL_USER_SETTING="preserved"\n')
            (seed/'custom_mod.yaml').write_text('# native trial user rules preserved\n')
            (seed/'unbound_custom.conf').write_text('server:\n    local-zone: "native-test.example." static\n    local-data: "native-test.example. 600 IN A 192.0.2.10"\n')
            (seed/'redis.conf').write_text('port 0\nunixsocket /tmp/redis.sock\nunixsocketperm 700\ndatabases 2\ndir /data\ndbfilename redis_dns_v2.rdb\nsave 900 1\nappendonly no\nmaxmemory 64mb\nmaxmemory-policy allkeys-lru\n')
            unbound=f'''server:
    interface: 0.0.0.0
    port: {{DNSPORT}}
    username: ""
    chroot: ""
    directory: "/tmp"
    use-syslog: no
    verbosity: 0
    do-ip6: no
    do-not-query-localhost: no
    local-zone: "test." transparent
    num-threads: 1
    module-config: "cachedb iterator"
    cache-max-ttl: 3600
    prefetch: no
    serve-expired: no
include-toplevel: "/data/unbound_custom.conf"
cachedb:
    backend: "redis"
    secret-seed: "native-trial-seed"
    redis-server-path: "/tmp/redis.sock"
#RAWDNS    redis-logical-db: 0
#CNAUTO    redis-logical-db: 1
    redis-timeout: 100
    redis-expire-records: no
forward-zone:
    name: "."
#RAWDNS    forward-addr: 127.0.0.1@{fixtures[0].port}
#CNAUTO    forward-addr: 127.0.0.1@{fixtures[1].port}
'''
            (seed/'unbound.conf').write_text(unbound)
            # Keep the actual native rendering, AddMod and supervisor pipeline.
            mosdns='''log:
  level: error
plugins:
  - tag: local_names
    type: domain_set
    args:
      exps: ["domain:local.cache.test"]
  - tag: local_backend
    type: forward
    args:
      upstreams: [{addr: "udp://127.0.0.1:5301"}]
  - tag: encrypted_backend
    type: forward
    args:
      upstreams: [{addr: "udp://127.0.0.1:5302"}]
  - tag: accept
    type: sequence
    args: [{exec: ok}]
  - tag: local_sequence
    type: sequence
    args: [{exec: "$local_backend"}, {exec: "goto accept"}]
  - tag: main
    type: sequence
    args:
      - exec: cache 256
      - matches: has_wanted_ans
        exec: goto accept
      - matches: qname $local_names
        exec: goto local_sequence
      - exec: $encrypted_backend
  - tag: udp_server
    type: udp_server
    args:
      entry: main
      listen: :53
  - tag: tcp_server
    type: tcp_server
    args:
      entry: main
      listen: :53
'''
            (seed/'mosdns.yaml').write_text(mosdns)
            dc=functions['config_text'](5302,https_port,None,512,3600)
            dc='\n'.join(line for line in dc.splitlines() if not line.startswith(('pqdnscrypt =','lb_strategy =')))+'\n'
            dc=dc.replace("[static.'fixture']","force_tcp = false\n#socksokproxy = 'socks5://{SOCKS5}'\n[static.'fixture']")
            (seed/'dnscrypt.toml').write_text(dc)
            config={**native['DEFAULTS'],'WEB_UI_TOKEN':'native-trial-admin','SUITE_READ_TOKEN':'native-trial-observer',
                'WEB_BIND_ADDRESS':'127.0.0.1','WEB_HOST_PORT':'18123','DNS_BIND_ADDRESS':'127.0.0.1','SUITE_DNS_PORT':'15353',
                'CNAUTO':'no','UPDATE':'no','CN_TRACKER':'no','USE_MARK_DATA':'no','CNFALL':'no','CACHE_BUDGET_MB':'128',
                'MEMORY_MAX_MB':str(args.memory_max_mb),'SOCKS5':f'127.0.0.1:{socks.server_address[1]}'}
            env=work/'service.env';native['write_env'](env,config)
            started=True
            command([sys.executable,package/'install.py','--instance',args.instance,'install','--config',env,'--import-data',seed],log=work/'install-control.log')
            for tcp in (False,True):
                if query(15353,'native-test.example',tcp=tcp)['answers'][0]['value']!='192.0.2.10':raise ValueError('Native recursive mode failed')
            record('clean-install-recursive',service=service(),queries=2)
            config['CNAUTO']='yes';native['write_env'](p['config'],config);restart()
            identity=app_probe();original_config=cfg_hashes();service_config=digest(p['config'])
            redis('SET','trial-marker','original-db0');redis('SET','trial-marker','original-db1',db=1)
            cachedb('control-cold-cache',fixtures,retained=False)
            redis('SAVE');restart();cachedb('control-rdb-reload',fixtures,retained=True)
            protocol('control-protocol',address,socks)
            split('control-split',fixtures,address)
            if cfg_hashes()!=original_config:raise ValueError('Control restart changed user templates')
            next_package=packages['packages']['combined']
            install_command('upgrade',args.packages/next_package['archive'],'--sha256',next_package['sha256'])
            home=(p['home']/'current').resolve();backup=Path(json.loads((p['home']/'rollback.json').read_text())['backup'])
            snapshot_hash=digest(backup/'data/redis_dns_v2.rdb')
            if (digest(home/'components/dnscrypt/dnscrypt-proxy')!=next_package['dnscrypt_binary_sha256']
                    or cfg_hashes()!=original_config or digest(p['config'])!=service_config or app_probe()!=identity):
                raise ValueError('Upgrade changed frozen identity, config or selected binary')
            frozen=json.loads((package/'manifest.json').read_text())['files']
            for name,checksum in frozen.items():
                if name.startswith(('core/','lib/','libexec/','dist/','wheels/')) and name!='core/dnscrypt-proxy' and digest(home/name)!=checksum:
                    raise ValueError('Upgrade changed a frozen component, library or shared payload')
            executable=[]
            for proc in Path('/proc').glob('[0-9]*/exe'):
                try:
                    if str(proc.resolve())==str(home/'components/dnscrypt/dnscrypt-proxy'):executable.append(int(proc.parent.name))
                except (FileNotFoundError,PermissionError,ProcessLookupError):pass
            if len(executable)!=2:raise ValueError('Both native DNSCrypt processes must run the selected payload')
            record('combined-installed',running_dnscrypt_pids=executable,binary_sha256=next_package['dnscrypt_binary_sha256'],identity_preserved=True,user_config_preserved=True,backup_rdb_sha256=snapshot_hash)
            cachedb('combined-old-rdb-reuse',fixtures,retained=True)
            protocol('combined-protocol',address,socks)
            split('combined-split',fixtures,address)
            new_questions=[('candidate-'+name,kind) for name,kind in questions[:12]]
            cachedb('combined-new-cache',fixtures,retained=False,question_set=new_questions)
            redis('SET','candidate-marker','new-db0');redis('SET','candidate-marker','new-db1',db=1)
            redis('SAVE');restart();cachedb('combined-rdb-reload',fixtures,retained=True)
            cachedb('combined-new-rdb-reload',fixtures,retained=True,question_set=new_questions)
            if [redis('GET','candidate-marker',db=db) for db in (0,1)]!=['new-db0','new-db1']:raise ValueError('Candidate writes did not persist')
            # An explicit rollback must restore the original snapshot, while
            # archiving candidate data for inspection.
            install_command('rollback')
            if ((p['home']/'current').resolve().name!=control['version'] or cfg_hashes()!=original_config
                    or digest(p['config'])!=service_config or app_probe()!=identity):raise ValueError('Explicit rollback identity/config failed')
            if any(redis('GET','candidate-marker',db=db) for db in (0,1)):raise ValueError('Rollback reused candidate data')
            if digest(backup/'data/redis_dns_v2.rdb')!=snapshot_hash:raise ValueError('Rollback modified original snapshot')
            cachedb('explicit-rollback-rdb',fixtures,retained=True)
            record('explicit-rollback',restored_control=True,original_snapshot_unchanged=True,candidate_data_archived=True)
            # Corrupt payload with a correct outer archive hash: installer must
            # reject the inner manifest before stopping or switching service.
            fault=work/'fault';fault.mkdir()
            extract_package(args.packages/next_package['archive'],fault)
            broken=next(fault.iterdir());wrapper=broken/'core/dnscrypt-proxy';wrapper.write_text(wrapper.read_text()+'# tampered\n')
            invalid=work/'bad-checksum.tar.gz'
            with tarfile.open(invalid,'w:gz') as archive:archive.add(broken,arcname=broken.name)
            before_service=service();before_home=(p['home']/'current').resolve()
            output=install_command('upgrade',invalid,'--sha256',digest(invalid),expected=1)
            if 'Package checksum differs' not in output or service()!=before_service or (p['home']/'current').resolve()!=before_home:
                raise ValueError('Corrupt package was not rejected before service switch')
            record('corrupt-package-rejected',service_pid_unchanged=True,release_unchanged=True)
            # Valid package with an intentional startup error exercises actual
            # post-switch automatic recovery, not just a mocked health error.
            fail_version=next_package['version']+'.failstart'
            wrapper.write_text('#!/bin/sh\nif [ "$1" = "-config" ]; then exit 77; fi\nbase=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\nexec "$base/components/dnscrypt/dnscrypt-proxy" "$@"\n');wrapper.chmod(0o755)
            manifest=json.loads((broken/'manifest.json').read_text());manifest['version']=fail_version;manifest['files']['core/dnscrypt-proxy']=digest(wrapper)
            (broken/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
            failed=work/'fail-start.tar.gz'
            with tarfile.open(failed,'w:gz') as archive:archive.add(broken,arcname=broken.name)
            output=install_command('upgrade',failed,'--sha256',digest(failed),expected=1)
            if 'Previous release and its data/configuration restored.' not in output:raise ValueError('Automatic rollback was not observed')
            if (p['home']/'current').resolve()!=before_home or cfg_hashes()!=original_config or digest(p['config'])!=service_config or app_probe()!=identity:
                raise ValueError('Automatic rollback failed identity or config checks')
            cachedb('automatic-rollback-rdb',fixtures,retained=True)
            record('failed-start-auto-recovered',restored_control=True,dns_healthy=True)
            result['passed']=True
        finally:
            if started:
                command(['systemctl','disable','--now',unit],log=work/'stop-trial.log')
                subprocess.run(['systemctl','reset-failed',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                result['final_service']=service()
                result['trial_stopped']=result['final_service']['ActiveState']=='inactive'
                result['data_preserved_for_review']=True
                (work/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    if not result.get('trial_stopped'):raise ValueError('Trial service did not stop')


if __name__=='__main__':
    main()
