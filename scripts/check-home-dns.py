"""Run a small read-only DNS check from a client, without changing its DNS settings."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import struct
import sys
import time

from dns_lab import question, read_name

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integrated/backend'))
from dns_wire import RECORD_TYPES, parse_response


def domain(value):
    try:
        value = value.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError as error:
        raise argparse.ArgumentTypeError('Use a valid DNS name') from error
    if (len(value) > 253 or not value or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.'))):
        raise argparse.ArgumentTypeError('Use a valid DNS name')
    return value


def receive(stream, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('DNS response deadline exceeded')
        stream.settimeout(remaining)
        block = stream.recv(size - len(data))
        if not block:
            raise ValueError('Incomplete DNS TCP response')
        data.extend(block)
    return bytes(data)


def exchange(packet, server, port, tcp, timeout):
    family = socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as stream:
        deadline = time.monotonic() + timeout
        stream.settimeout(timeout)
        stream.connect((server, port))
        if tcp:
            stream.sendall(struct.pack('!H', len(packet)) + packet)
            length = struct.unpack('!H', receive(stream, 2, deadline))[0]
            return receive(stream, length, deadline)
        stream.send(packet)
        return stream.recv(65535)


def matching(answers, name, kind):
    visited = set()
    while name not in visited:
        visited.add(name)
        records = [r for r in answers if r['name'].lower().rstrip('.') == name and r['class'] == 1]
        found = [r for r in records if r['type'] == kind]
        if found:
            return found
        aliases = [r for r in records if r['type'] == 'CNAME']
        if not aliases:
            break
        name = aliases[0]['value'].lower().rstrip('.')
    return []


def probe(server, port, name, kind, transport, timeout):
    packet = question(name, RECORD_TYPES[kind])
    row = {'domain': name, 'record_type': kind, 'transport': transport,
           'checked_at': datetime.now(timezone.utc).isoformat(), 'responded': False,
           'available': False, 'records': [], 'tcp_fallback': False}
    began = time.monotonic()
    try:
        decode = lambda raw: parse_response(raw, int.from_bytes(packet[:2], 'big'), name, kind, read_name)
        response = decode(exchange(packet, server, port, transport == 'tcp', timeout))
        row['responded'] = True
        if response['truncated'] and transport == 'udp':
            row['tcp_fallback'] = True
            response = decode(exchange(packet, server, port, True, timeout))
        records = matching(response['answers'], name, kind)
        status = ('incomplete' if response['truncated'] else 'dns_error' if response['rcode_value']
                  else 'ok' if records else 'no_records')
        row.update(status=status, rcode=response['rcode'], available=status == 'ok',
                   records=records, cname_chain=[r for r in response['answers'] if r['type'] == 'CNAME'])
    except (OSError, ValueError, IndexError, struct.error) as error:
        row.update(status='timeout' if isinstance(error, TimeoutError) else
                   'network_error' if isinstance(error, OSError) else 'invalid_response', error=str(error))
    row['elapsed_ms'] = (time.monotonic() - began) * 1000
    return row


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--server', required=True, type=ipaddress.ip_address, help='Literal server IP')
    cli.add_argument('--port', type=int, default=53)
    cli.add_argument('--domain', action='append', type=domain, required=True)
    cli.add_argument('--record-type', choices=('A', 'AAAA'), action='append', default=None)
    cli.add_argument('--rounds', type=int, choices=(1, 2, 3), default=2)
    cli.add_argument('--timeout', type=float, default=3)
    cli.add_argument('--output', type=Path, required=True, help='New private JSON report; never overwritten')
    args = cli.parse_args()
    if not 1 <= args.port <= 65535 or not 0 < args.timeout <= 10 or len(args.domain) > 6:
        cli.error('Use a valid port, timeout in (0, 10], and at most six domains')
    if args.output.exists() or args.output.is_symlink():
        cli.error('The report path must be new')
    names = list(dict.fromkeys(args.domain))
    kinds = list(dict.fromkeys(args.record_type or ['A']))
    report = {'schema': 'lovestdns-home-client-check-v1',
              'server': str(args.server), 'port': args.port, 'dns_settings_changed': False,
              'private_report': True, 'functional_only': True, 'component_versions_verified': False,
              'method_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'state': 'running', 'passed': False,
              'expected_queries': args.rounds * len(names) * len(kinds) * 2, 'queries': []}
    # Reserve the destination before sending any query. Interrupted runs retain their completed rows.
    with args.output.open('x', encoding='utf-8') as out:
        for round_index in range(args.rounds):
            for name in names:
                for kind in kinds:
                    for transport in ('udp', 'tcp'):
                        row = probe(str(args.server), args.port, name, kind, transport, args.timeout)
                        report['queries'].append({'round': round_index + 1, **row})
                        report['counts'] = dict(Counter(r['status'] for r in report['queries']))
                        out.seek(0)
                        json.dump(report, out, ensure_ascii=False, indent=2)
                        out.write('\n')
                        out.truncate()
                        out.flush()
        report.update(state='complete', passed=all(r['available'] for r in report['queries']))
        out.seek(0)
        json.dump(report, out, ensure_ascii=False, indent=2)
        out.write('\n')
        out.truncate()
        print(json.dumps({k: report[k] for k in ('passed', 'counts', 'dns_settings_changed')}))
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
