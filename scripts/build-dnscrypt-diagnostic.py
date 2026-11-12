"""Build disposable DNSCrypt profiling binaries, never a deployable component bundle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

SOURCES = {
    '2.1.5': ('044c4db9a3c7bdcf886ff8f83c4b137d2fd37a65477a92bfe86bf69587ea7355', 'go1.22.5'),
    '2.1.20': ('2e6adb32e0f91cb755f1d2c060cddcc80ffca6a181bf03c90df22bbade121607', 'go1.27.3'),
}
BUFFER_PATCH = Path(__file__).resolve().parents[1] / 'packaging/components/patches/dnscrypt-2.1.20-udp-receive-buffer.patch'
PACKET_PATCH = BUFFER_PATCH.with_name('dnscrypt-2.1.20-udp-packet-allocation.patch')

# Added only to a verified source copy below the diagnostic work directory.
# CPU and heap runs are separate; the regular program disables heap sampling.
INSTRUMENTATION = '''package main

import (
    "fmt"
    "net"
    "net/http"
    "net/http/pprof"
    "os"
    "runtime"
    "time"
)

func lovestdnsDiagnostics() {
    address := os.Getenv("LOVESTDNS_DIAGNOSTICS_ADDR")
    host, _, err := net.SplitHostPort(address)
    if err != nil || host != "127.0.0.1" {
        panic("diagnostic binary requires a literal 127.0.0.1 listener")
    }
    switch os.Getenv("LOVESTDNS_DIAGNOSTICS_MODE") {
    case "cpu":
        runtime.MemProfileRate = 0
    case "heap":
        runtime.MemProfileRate = 64 * 1024
    default:
        panic("diagnostic binary requires cpu or heap mode")
    }
    listener, err := net.Listen("tcp4", address)
    if err != nil { panic(err) }
    mux := http.NewServeMux()
    mux.HandleFunc("/debug/pprof/profile", pprof.Profile)
    mux.Handle("/debug/pprof/heap", pprof.Handler("heap"))
    server := &http.Server{Handler: mux, ReadHeaderTimeout: 5*time.Second}
    fmt.Fprintln(os.Stderr, "DISPOSABLE DIAGNOSTIC BUILD: not for deployment")
    go func() { if err := server.Serve(listener); err != nil { panic(err) } }()
}
'''


def instrument(source):
    main = source / 'dnscrypt-proxy/main.go'
    text = main.read_text()
    marker = '\truntime.MemProfileRate = 0'
    if text.count(marker) != 1:
        raise ValueError('Expected exactly one heap sampler setup in verified source')
    main.write_text(text.replace(marker, marker + '\n\tlovestdnsDiagnostics()'))
    (main.parent / 'lovestdns_diagnostics.go').write_text(INSTRUMENTATION)


def build(archive, go, work, version, buffer_patch=False, packet_patch=False):
    if (buffer_patch or packet_patch) and version != '2.1.20':
        raise ValueError('The UDP patches are reviewed only for 2.1.20')
    expected, toolchain = SOURCES[version]
    archive = archive.resolve()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('Source archive differs from the reviewed upstream tag')
    go = go.resolve()
    env = {**os.environ, 'GOTOOLCHAIN': 'local', 'GOWORK': 'off', 'GOFLAGS': '',
           'CGO_ENABLED': '0', 'GOOS': 'linux', 'GOARCH': 'amd64', 'GOAMD64': 'v1', 'GOMAXPROCS': '2'}
    actual = subprocess.check_output([str(go), 'version'], env=env, text=True).strip()
    if actual != f'go version {toolchain} linux/amd64':
        raise ValueError('Unexpected diagnostic toolchain: ' + actual)
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as src:
        src.extractall(work, filter='data')
    source = work / ('dnscrypt-proxy-' + version)
    patches = [p for enabled, p in ((buffer_patch, BUFFER_PATCH), (packet_patch, PACKET_PATCH)) if enabled]
    patch_digests = {}
    for patch in patches:
        patch_digests[patch.name] = hashlib.sha256(patch.read_bytes()).hexdigest()
        with (work / (patch.stem + '.log')).open('w') as log:
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch.resolve())],
                           cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=10)
    instrument(source)
    binary = work / 'dnscrypt-diagnostic'
    command = [str(go), 'build', '-mod=vendor', '-trimpath', '-buildvcs=false', '-o', str(binary), './dnscrypt-proxy']
    with (work / 'build.log').open('w') as log:
        subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    manifest = {'diagnostic_only': True, 'deployable': False, 'version': version,
                'source_url': 'https://codeload.github.com/DNSCrypt/dnscrypt-proxy/tar.gz/refs/tags/' + version,
                'source_sha256': expected, 'toolchain': actual, 'build_command': command,
                'patches_sha256': patch_digests,
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                'instrumentation_sha256': hashlib.sha256(INSTRUMENTATION.encode()).hexdigest(),
                'limitations': ['Rebuilt with CGO_ENABLED=0; old deployed binary uses musl/PIE/CGO.',
                                'Profiling instrumentation and symbols differ from official binaries.',
                                'Profiles locate hotspots; their latency/CPU/RSS are not release acceptance results.']}
    (work / 'diagnostic-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-archive', type=Path, required=True)
    parser.add_argument('--go', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', choices=SOURCES, required=True)
    parser.add_argument('--buffer-patch', action='store_true', help='Apply the isolated 2.1.20 receive buffer patch')
    parser.add_argument('--packet-patch', action='store_true', help='Apply the isolated 2.1.20 packet allocation patch')
    args = parser.parse_args()
    print(json.dumps(build(args.source_archive, args.go, args.work, args.version, args.buffer_patch, args.packet_patch), indent=2))


if __name__ == '__main__':
    main()
