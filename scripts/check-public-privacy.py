"""Reject private deployment evidence and personal credentials before publication."""
import argparse
from io import BytesIO
import ipaddress
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PREFIXES = ('.build/', '.agents/', '.codex/', 'docs/validation/', 'data/', 'state/', 'logs/')
PRIVATE_SUFFIXES = ('.rdb', '.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.log')
PATTERNS = {
    'unpublished project name': re.compile(rb'(?i)home' rb'net|\bhnm\b'),
    'deployment host label': re.compile(rb'(?i)\bserver\d+(?:-vm-debian)?\b|\bFRP-\d+\b'),
    'private host workdir': re.compile(rb'/root/lovestdns-(?:validation|migration|ui|status)[\w/-]*'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----'),
    'credential': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,})\b'),
    'local source path': re.compile(rb'(?i)[A-Z]:[\\/](?:Users|workspace)[\\/]'),
    'personal email': re.compile(rb'(?i)[A-Z0-9._%+-]+@(?:gmail|outlook|hotmail|qq|163|126|icloud)\.[A-Z.]+'),
}
IPV4 = re.compile(rb'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
# Public upstream examples/networks are retained in the frozen standalone tree.
EXAMPLE_IPS = {'10.0.0.0', '172.16.0.0', '192.168.0.0', '192.168.1.0', '192.168.1.1',
               '192.168.1.2', '192.168.1.8', '10.9.8.7', '10.10.10.3', '10.10.10.4',
               '10.10.10.5', '10.10.10.6', '10.10.10.8', '10.111.222.11',
               '10.111.222.12', '10.111.222.13', '10.111.222.14'}
PRIVATE_NETS = tuple(ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def git(*args, data=None):
    return subprocess.check_output(['git', *args], cwd=ROOT, input=data)


def private_name(name):
    base = Path(name).name
    return (name.startswith(PRIVATE_PREFIXES) or name.endswith(PRIVATE_SUFFIXES)
            or (base.startswith('.env') and not base.endswith('.example') and base != '.env.example'))


def inspect(name, data):
    findings = []
    if private_name(name):
        findings.append((name, 'private file'))
    if data.startswith(b'PK\x03\x04'):
        with zipfile.ZipFile(BytesIO(data)) as archive:
            for item in archive.infolist():
                if not item.is_dir():
                    findings.extend(inspect(name + '::' + item.filename, archive.read(item)))
        return findings
    if b'\0' in data:
        return findings
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            findings.append((name, label))
    if not name.endswith('check-public-privacy.py'):
        for match in IPV4.finditer(data):
            value = match.group().decode()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if value not in EXAMPLE_IPS and any(address in network for network in PRIVATE_NETS):
                findings.append((name, 'non-example private address'))
                break
    return findings


def scan(refs, history=False, staged=False):
    findings = []
    objects = {}
    if history:
        for ref in refs:
            metadata = git('log', ref, '--format=%B%n%an <%ae>%n%cn <%ce>')
            findings.extend(inspect('commit metadata', metadata))
            if git('cat-file', '-t', ref).strip() == b'tag':
                findings.extend(inspect('tag metadata', git('cat-file', 'tag', ref)))
            # Only objects reachable from the refs being published are scanned.
            for line in git('rev-list', '--objects', ref).splitlines():
                sha, _, name = line.partition(b' ')
                if name:
                    objects[sha] = name.decode('utf-8', errors='replace')
    else:
        for ref in refs:
            args = ('ls-files', '--stage', '-z') if staged else ('ls-tree', '-rz', ref)
            for item in git(*args).split(b'\0'):
                if not item:
                    continue
                entry, name = item.split(b'\t', 1)
                sha = entry.split()[1 if staged else 2]
                objects[sha] = name.decode('utf-8', errors='replace')
    if objects:
        checks = git('cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)', data=b'\n'.join(objects) + b'\n')
        blobs = [(sha, int(size)) for sha, kind, size in (line.split() for line in checks.splitlines()) if kind == b'blob']
        raw = git('cat-file', '--batch', data=b'\n'.join(sha for sha, _ in blobs) + b'\n')
        cursor = 0
        for sha, size in blobs:
            start = raw.index(b'\n', cursor) + 1
            data = raw[start:start + size]
            cursor = start + size + 1
            findings.extend(inspect(objects[sha], data))
    return sorted(set(findings)), len(objects)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--ref', action='append', default=[])
    parser.add_argument('--all-public-refs', action='store_true')
    args = parser.parse_args()
    refs = args.ref
    if args.all_public_refs:
        refs += git('for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/tags').decode().splitlines()
    findings, count = scan(refs or ['HEAD'], args.history, args.staged)
    if findings:
        for name, reason in findings:
            print(f'BLOCKED: {reason}: {name}', file=sys.stderr)
        raise SystemExit(1)
    print(f'Public privacy check passed ({count} objects; history={args.history})')


if __name__ == '__main__':
    main()
