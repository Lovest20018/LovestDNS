"""Build and verify the small MosDNS entry extension from the frozen binary's source commit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'packaging/mosdns-filter.lock.json'
EXTENSION = ROOT / 'packaging/components/mosdns-filter'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def locked_source():
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    return {'source_commit': lock['source_commit'], 'source_sha256': lock['source_sha256'],
            'go_version': lock['go_version'], 'hook_sha256': digest(Path(__file__)),
            'extension': {p.name: digest(p) for p in sorted(EXTENSION.glob('*.go'))}}


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Frozen MosDNS entry changed: ' + old[:80])
    return text.replace(old, new, 1)


def patch(source):
    shutil.copytree(EXTENSION, source / 'pkg/dnsfilter')
    for transport in ('udp', 'tcp'):
        path = source / f'pkg/server/{transport}.go'
        text = path.read_text(encoding='utf-8')
        text = replace(text, '"context"', '"context"\n\t"github.com/IrineSistiana/mosdns/v5/pkg/dnsfilter"')
        if transport == 'udp':
            signature = 'func (s *UDPServer) ServeUDP(c net.PacketConn) error {'
            args = '"udp", c.LocalAddr()'
            query, context, writer = 'q', 'listenerCtx', 'cmc.writeTo(b, localAddr, ifIndex, remoteAddr)'
            endpoint = '"udp", remoteAddr, c.LocalAddr(), localAddr'
            write_error = '\n\t\t\t\t\ts.opts.Logger.Warn("failed to write response", zap.Stringer("client", remoteAddr), zap.Error(err))\n\t\t\t\t}'
            indent, write_indent = '\t\t\t', '\t\t\t\t'
        else:
            signature = 'func (s *TCPServer) ServeTCP(l net.Listener) error {'
            args = '"tcp", l.Addr()'
            query, context, writer = 'req', 'tcpConnCtx', 'dnsutils.WriteRawMsgToTCP(c, b)'
            endpoint = '"tcp", c.RemoteAddr(), c.LocalAddr(), nil'
            write_error = '\n\t\t\t\t\t\ts.opts.Logger.Warn("failed to write response", zap.Stringer("client", c.RemoteAddr()), zap.Error(err))\n\t\t\t\t\t\treturn\n\t\t\t\t\t}'
            indent, write_indent = '\t\t\t\t\t', '\t\t\t\t\t'
        text = replace(text, signature, signature + f'\n\tfilter := dnsfilter.ForListener({args})')
        serve = f'if err := s.opts.DNSHandler.ServeDNS({context}, qCtx); err != nil {{'
        text = replace(text, indent + serve, indent + f'evaluation, filtered := filter.Begin({query})\n'
                       + indent + 'if filtered != nil {\n' + indent + '\tqCtx.SetResponse(filtered)\n'
                       + indent + '} else ' + serve)
        write = f'if _, err := {writer}; err != nil {{' + write_error
        record = f'filter.Record(evaluation, {endpoint}, {query}, r)'
        failure = f'filter.Record(evaluation, {endpoint}, {query}, nil)'
        if transport == 'udp':
            text = replace(text, write, write[:-1] + failure + '\n' + write_indent
                           + '} else {\n' + write_indent + '\t' + record + '\n' + write_indent + '}')
            # Keep the failure publication aligned with the logger.
            text = text.replace('\n\t\t\t\t' + failure, '\n\t\t\t\t\t' + failure)
        else:
            text = replace(text, write, write.replace(write_indent + '\treturn', write_indent + '\t' + failure + '\n' + write_indent + '\treturn')
                           + '\n' + write_indent + record)
        path.write_text(text, encoding='utf-8', newline='\n')


def build(output, go, archive=None, *, host_test=False, baseline=False):
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if subprocess.check_output([go, 'version'], text=True).split()[2] != lock['go_version']:
        raise ValueError('Go toolchain must match the MosDNS filter lock')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mosdns-filter-', dir=output) as work:
        work = Path(work)
        if archive is None:
            archive = work / 'source.tar.gz'
            with urllib.request.urlopen(lock['source_url'], timeout=30) as response, archive.open('wb') as file:
                shutil.copyfileobj(response, file)
        if digest(archive) != lock['source_sha256']:
            raise ValueError('MosDNS source archive checksum mismatch')
        with tarfile.open(archive) as bundle:
            bundle.extractall(work, filter='data')
        source = work / ('mosdns-' + lock['source_commit'])
        if not baseline:
            patch(source)
        system = platform.system().lower() if host_test else 'linux'
        name = ('mosdns-baseline' if baseline else 'mosdns-filter') + ('.exe' if system == 'windows' else '')
        target = output / name
        environment = {**os.environ, 'GOOS': system, 'GOARCH': 'amd64', 'CGO_ENABLED': '0',
                       'GOTOOLCHAIN': 'local', 'GOAMD64': 'v1', 'GOWORK': 'off', 'GOFLAGS': ''}
        subprocess.run([go, 'build', '-mod=readonly', '-trimpath', '-buildvcs=false',
                        '-o', str(target), '.'], cwd=source, env=environment, check=True)
        if not baseline:
            manifest = {**locked_source(), 'target': system + '/amd64', 'sha256': digest(target)}
            (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return target


def install(directory, destination, provenance):
    directory = Path(directory)
    binary = directory / 'mosdns-filter'
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest != {**locked_source(), 'target': 'linux/amd64', 'sha256': digest(binary)}:
        raise ValueError('MosDNS filter manifest does not match the locked build')
    with binary.open('rb') as stream:
        header = stream.read(20)
    if not header.startswith(b'\x7fELF\x02\x01') or header[18:20] != b'\x3e\x00':
        raise ValueError('MosDNS filter is not a Linux amd64 binary')
    shutil.copyfile(binary, destination)
    Path(destination).chmod(0o755)
    shutil.copyfile(directory / 'manifest.json', provenance)
    return manifest


def install_container(root):
    root = Path(root)
    info = root / 'build-info.json'
    value = json.loads(info.read_text(encoding='utf-8'))
    value['mosdns_filter'] = install(root / 'filter-input', root / 'components/mosdns-filter',
                                    root / 'components/mosdns-filter.json')
    info.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    shutil.rmtree(root / 'filter-input')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path)
    mode.add_argument('--install-container', type=Path)
    parser.add_argument('--go', default='go')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--host-test', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    if args.install_container:
        install_container(args.install_container)
    else:
        print(build(args.output, args.go, args.archive, host_test=args.host_test, baseline=args.baseline))
