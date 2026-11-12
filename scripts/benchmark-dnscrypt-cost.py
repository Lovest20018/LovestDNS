"""Measure verified DNS parser/cache code with fixed work and process CPU time."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'packaging/components/tests/dnscrypt_cost_test.go'
MATRIX_FIXTURE = ROOT / 'packaging/components/tests/dnscrypt_cache_matrix_test.go'
LOCK = ROOT / 'packaging/dnscrypt-cost.lock.json'
PATCHES = ROOT / 'packaging/components/patches'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def function(source, name):
    """These top-level functions have a single unindented closing brace."""
    marker = 'func ' + name + '('
    if source.count(marker) != 1:
        raise ValueError('Expected exactly one upstream function: ' + name)
    start = source.index(marker)
    return source[start:source.index('\n}', start) + 2] + '\n'


def adapter(version):
    old = version == '2.1.5'
    library = 'github.com/miekg/dns' if old else 'codeberg.org/miekg/dns'
    declaration = '''package main
import ("DNS_LIBRARY")
type Proxy struct { cacheSize int }
type PluginsState struct {
    dnssec, cacheHit bool
    sessionData map[string]any
    synthResponse *dns.Msg
    action int
    cacheSize int
    cacheMinTTL, cacheMaxTTL, cacheNegMinTTL, cacheNegMaxTTL uint32
}
const PluginsActionSynth = 4
type costMsg = dns.Msg
const costCDIsolated = CD_ISOLATED
func costReset() { RESET }
func costUnpack(packet []byte) *dns.Msg { UNPACK }
func costPack(msg *dns.Msg) []byte { PACK }
func costClone(msg *dns.Msg) *dns.Msg { CLONE }
func costTTL(msg *dns.Msg) uint32 { TTL }
func costChangeTTL(msg *dns.Msg, ttl uint32) { CHANGE_TTL }
func costSetCD(msg *dns.Msg, flag bool) { msg.CheckingDisabled = flag }
func costTTLState() *PluginsState { return &PluginsState{cacheSize:512, cacheMaxTTL:600,
    cacheNegMaxTTL:600, sessionData:make(map[string]any)} }
'''.replace('DNS_LIBRARY', library).replace('CD_ISOLATED', 'false' if old else 'true')
    replacements = {
        'RESET': ('cachedResponses = CachedResponses{}' if old else
                  'cachedResponses = nil; if err := initCachedResponses(512); err != nil { panic(err) }'),
        'UNPACK': ('m:=new(dns.Msg); if err:=m.Unpack(packet);err!=nil{panic(err)};return m' if old else
                   'm:=&dns.Msg{Data:packet};if err:=m.Unpack();err!=nil{panic(err)};return m'),
        'PACK': ('b,err:=msg.Pack();if err!=nil{panic(err)};return b' if old else
                 'if err:=msg.Pack();err!=nil{panic(err)};return msg.Data'),
        'CLONE': 'return msg.Copy()' if old else 'return cloneMsg(msg)',
        'TTL': 'return msg.Answer[0].Header().Ttl' if old else 'return msg.Answer[0].Header().TTL',
        'CHANGE_TTL': 'msg.Answer[0].Header().Ttl=ttl' if old else 'msg.Answer[0].Header().TTL=ttl'}
    for token, body in replacements.items():
        declaration = declaration.replace('{ ' + token + ' }', '{ ' + body + ' }')
    return declaration, library


def prepare(source_archive, toolchain_archive, work, case, cache_matrix=False):
    lock = json.loads(LOCK.read_text())
    version, toolchain = lock['cases'][case]
    if cache_matrix and version != '2.1.20':
        raise ValueError('Cache matrix requires the locked 2.1.20 source')
    inputs = [(source_archive, lock['sources'][version]),
              (toolchain_archive, lock['toolchains'][toolchain])]
    patches = lock.get('case_patches', {}).get(case, [])
    inputs += [(PATCHES / name, lock['patches'][name]) for name in patches]
    for path, expected in inputs:
        if digest(path) != expected:
            raise ValueError('Cost input digest mismatch: ' + str(path))
    work.mkdir(parents=True, exist_ok=False)
    for path in (source_archive, toolchain_archive):
        with tarfile.open(path) as archive:
            archive.extractall(work, filter='data')
    source = work / ('dnscrypt-proxy-' + version)
    for name in patches:
        with (work / (name + '.log')).open('w') as log:
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCHES / name)],
                           cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=10)
    package = source / 'dnscrypt-proxy'
    selected = work / 'selected'
    selected.mkdir()
    # Pass named Go files so unrelated transport, QUIC and monitoring code is
    # not linked or altered to make an older release compile with a newer Go.
    text, library = adapter(version)
    (selected / 'adapter.go').write_text(text)
    upstream = package / 'dnsutils.go'
    names = ['NormalizeRawQName', 'getMinTTL', 'updateTTL']
    if version != '2.1.5':
        names += ['cloneRRs', 'cloneMsg']
    functions = '\n'.join(function(upstream.read_text(), name) for name in names)
    (selected / 'helpers.go').write_text('package main\nimport("time";"' + library + '")\n' + functions)
    shutil.copyfile(package / 'plugin_cache.go', selected / 'cache.go')
    shutil.copyfile(FIXTURE, selected / 'cost_test.go')
    if cache_matrix:
        shutil.copyfile(MATRIX_FIXTURE, selected / 'cache_matrix_test.go')
    return lock, source, selected, toolchain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--toolchain-archive', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--case', choices=json.loads(LOCK.read_text())['cases'], required=True)
    parser.add_argument('--cache-matrix', action='store_true', help='Include 2.1.20 capacity, expiry and parallel fixtures')
    args = parser.parse_args()
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Requires a disposable network namespace; this benchmark sends no packets')
    work = args.work.resolve()
    lock, source, selected, toolchain = prepare(args.source_archive.resolve(), args.toolchain_archive.resolve(), work, args.case, args.cache_matrix)
    go = work / 'go/bin/go'
    env = {**os.environ, 'GOTOOLCHAIN':'local', 'GOWORK':'off', 'GOFLAGS':'', 'GOEXPERIMENT':'',
           'CGO_ENABLED':'0', 'GOARCH':'amd64', 'GOOS':'linux', 'GOAMD64':'v1', 'GOMAXPROCS':'2',
           'GOPROXY':'off', 'GOSUMDB':'off', 'GOROOT':str(work/'go')}
    if subprocess.check_output([str(go), 'version'], env=env, text=True).strip() != f'go version {toolchain} linux/amd64':
        raise ValueError('Unexpected cost benchmark toolchain')
    binary = work / 'cost.test'
    files = ['cache.go','helpers.go','adapter.go','cost_test.go']
    if args.cache_matrix:
        files.append('cache_matrix_test.go')
    command = [str(go), 'test', '-mod=vendor', '-trimpath', '-buildvcs=false', '-c', '-o', str(binary),
               *(str(selected / name) for name in files)]
    with (work / 'build.log').open('w') as stream:
        subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=600)
    manifest = {'diagnostic_only':True, 'deployable':False, 'case':args.case, 'source_version':lock['cases'][args.case][0],
                'cache_matrix':args.cache_matrix,
                'source_sha256':digest(args.source_archive), 'toolchain':toolchain,
                'toolchain_archive_sha256':digest(args.toolchain_archive), 'binary_sha256':digest(binary),
                'patches_sha256':{name:lock['patches'][name] for name in lock.get('case_patches',{}).get(args.case,[])},
                'selected_files_sha256':{p.name:digest(p) for p in selected.iterdir()},
                'lock_sha256':digest(LOCK), 'runner_sha256':digest(Path(__file__)),
                'limitations':['Extracted upstream cache and TTL/clone helpers; minimal state types adapt only their dependencies.',
                               'Fixed-work process CPU and allocations; excludes transport, socket, scheduler idle costs and full validation pipeline.',
                               'Not a deployable full DNSCrypt build and not release performance acceptance.']}
    (work / 'cost-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    with (work / 'contracts.log').open('w') as stream:
        subprocess.run([str(binary), '-test.v', '-test.run=^TestCost', '-test.timeout=30s'],
                       cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=40)
    # Execution is separate so the controller can interleave cases after every
    # build has finished. No measurement is made during compilation.
    print(json.dumps(manifest), flush=True)


if __name__ == '__main__':
    main()
