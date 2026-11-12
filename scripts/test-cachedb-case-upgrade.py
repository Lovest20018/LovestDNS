"""Exercise offline cachedb aliases through real old/new Unbound and Redis."""
import argparse
import base64
import json
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

from cachedb_case_compat import Redis, file_hash, lookup_key, parse_value
from dns_lab import CacheLab, Fixture, candidate_command, exact, parse, question, read_name

ROOT = Path(__file__).resolve().parents[1]
SEED = b'compatibility-test-seed'


def wire(name):
    return question(name, identifier=0)[12:-4]


def rr(name, kind, ttl, value):
    return wire(name) + struct.pack('!HHIH', kind, 1, ttl, len(value)) + value


def ask(port, name, kind=1, *, tcp=False, dnssec=False):
    packet = question(name, kind)
    if dnssec:
        packet = packet[:10] + b'\0\1' + packet[12:] + b'\0' + struct.pack('!HHIH', 41, 1232, 0x8000, 0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as stream:
        stream.settimeout(3)
        stream.connect(('127.0.0.1', port))
        if tcp:
            stream.sendall(struct.pack('!H', len(packet)) + packet)
            answer = exact(stream, struct.unpack('!H', exact(stream, 2))[0])
        else:
            stream.send(packet)
            answer = stream.recv(65535)
    result = parse(answer)
    assert result['id'] == int.from_bytes(packet[:2], 'big')
    result['authenticated'] = bool(int.from_bytes(answer[2:4], 'big') & 0x20)
    return result


class SignedFixture(Fixture):
    """Private RSA trust anchor and real signatures, including one invalid A RRset."""
    def __init__(self, directory, address):
        self.key = directory / ('key-' + address.replace('.', '-') + '.pem')
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(self.key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.key.chmod(0o600)
        modulus = bytes.fromhex(subprocess.check_output(['openssl', 'rsa', '-in', str(self.key),
                                                       '-noout', '-modulus'], text=True).strip().split('=')[1])
        self.dnskey = struct.pack('!HBB', 257, 3, 8) + b'\x03\x01\x00\x01' + modulus
        checksum = sum(byte << (8 if index % 2 == 0 else 0) for index, byte in enumerate(self.dnskey))
        checksum += (checksum >> 16) & 0xffff
        self.keytag = checksum & 0xffff
        self.anchor = 'secure.cache.test. IN DNSKEY 257 3 8 ' + base64.b64encode(self.dnskey[4:]).decode()
        self.signed = {}
        self.inception, self.expiration = int(time.time()) - 60, int(time.time()) + 3600
        self.short_expiration = int(time.time()) + 15
        super().__init__(address, ttl=600, threaded_udp=False, track_questions=True)

    def signed_rr(self, name, kind, value, *, corrupt=False):
        key = name, kind, value, corrupt
        if key not in self.signed:
            expiration = self.short_expiration if name.startswith('expired.') else self.expiration
            head = struct.pack('!HBBIIIH', kind, 8, len(name.split('.')), 600,
                               expiration, self.inception, self.keytag) + wire('secure.cache.test')
            signature = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(self.key)],
                                                input=head + rr(name, kind, 600, value))
            if corrupt:
                signature = bytes([signature[0] ^ 1]) + signature[1:]
            self.signed[key] = rr(name, kind, 600, value) + rr(name, 46, 600, head + signature)
        return self.signed[key]

    def respond(self, packet, tcp):
        name, end = read_name(packet, 12)
        kind, _ = struct.unpack('!HH', packet[end:end + 4])
        lower = name.lower()
        if lower.endswith('.secure.cache.test') or lower == 'secure.cache.test':
            with self.lock:
                self.counts[name, 'tcp' if tcp else 'udp'] += 1
                self.question_counts[name.lower(), kind, 1, 'tcp' if tcp else 'udp'] += 1
            if lower == 'secure.cache.test' and kind == 48:
                answer = self.signed_rr(lower, 48, self.dnskey)
            elif kind == 1 and lower in ('signed.secure.cache.test', 'bogus.secure.cache.test', 'expired.secure.cache.test'):
                answer = self.signed_rr(lower, 1, socket.inet_aton(self.address), corrupt=lower.startswith('bogus.'))
            else:
                answer = b''
            return (struct.pack('!6H', int.from_bytes(packet[:2], 'big'), 0x8580, 1,
                                2 if answer else 0, 0, 0) + packet[12:end + 4] + answer)
        # Canonicalize only fixture routing. Echo the original question bytes.
        with self.lock:
            self.counts[name, 'tcp' if tcp else 'udp'] += 1
            self.question_counts[name.lower(), kind, 1, 'tcp' if tcp else 'udp'] += 1
        missing, nodata = lower.startswith('missing.'), lower.startswith('nodata.')
        ttl = 2 if lower.startswith('short.') else 600
        if missing or nodata:
            soa = wire('ns.cache.test') + wire('hostmaster.cache.test') + struct.pack('!5I', 1, 60, 60, 600, 2)
            answer, authority, count = b'', rr('cache.test', 6, 2, soa), 0
        elif kind in (1, 28, 16):
            value = (socket.inet_aton(self.address) if kind == 1 else
                     socket.inet_pton(socket.AF_INET6, '2001:db8::10') if kind == 28 else b'\x07fixture')
            if lower.startswith('cname.') or lower.startswith('partial.'):
                answer = rr(lower, 5, ttl, wire('target.cache.test')) + rr('target.cache.test', kind, ttl, value)
                count = 2
            else:
                answer, count = rr(lower, kind, ttl, value), 1
            authority = b''
        else:
            answer, authority, count = b'', b'', 0
        return (struct.pack('!6H', int.from_bytes(packet[:2], 'big'), 0x8583 if missing else 0x8580,
                            1, count, 1 if authority else 0, 0) + packet[12:end + 4] + answer + authority)


class ValidatingLab(CacheLab):
    def start_unbound(self, db):
        config = self.root / f'unbound-{db}.conf'
        text = config.read_text()
        if 'trust-anchor:' not in text:
            text = text.replace('module-config: "cachedb iterator"', 'module-config: "validator cachedb iterator"')
            text = text.replace('    cache-max-ttl:', '    trust-anchor-signaling: no\n    cache-max-ttl:')
            text = text.replace('    cache-max-ttl:', '    val-sig-skew-min: 0\n    val-sig-skew-max: 0\n    cache-max-ttl:')
            text = text.replace('    cache-max-ttl:', '    trust-anchor: "' + self.fixtures[db].anchor + '"\n    cache-max-ttl:')
            config.write_text(text)
        super().start_unbound(db)


def lab(root, snapshot, commands, fixtures):
    if snapshot:
        root.mkdir()
        shutil.copyfile(snapshot, root / 'cache.rdb')
    # Use the same exception-safe construction as running_lab, with a validator.
    import contextlib

    @contextlib.contextmanager
    def context():
        obj = ValidatingLab.__new__(ValidatingLab)
        obj.children = []
        try:
            obj.__init__(root, commands['redis-server'], commands['redis-cli'], commands['unbound'], fixtures)
            yield obj
        finally:
            obj.close()
    return context()


def contents(client):
    result = {}
    for db in (0, 1):
        client.call('SELECT', db)
        cursor, values = b'0', {}
        while True:
            cursor, keys = client.call('SCAN', cursor, 'COUNT', 512)
            for key in keys:
                values[key] = (client.call('DUMP', key), client.call('PEXPIRETIME', key))
            if cursor == b'0':
                break
        result[db] = values
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'bundle', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.work.exists():
        parser.error('Use a fresh disposable work directory')
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, mode=0o700)
    old = {n: [str((args.baseline_home / 'core' / n).resolve())] for n in ('redis-server', 'redis-cli', 'unbound')}
    new = {n: candidate_command(args.bundle, 'unbound' if n == 'unbound' else 'redis', n) for n in old}
    fixtures = [SignedFixture(args.work, '192.0.2.10'), SignedFixture(args.work, '192.0.2.20')]
    result = {'passed': False, 'scope': 'Synthetic signed/unsigned DNS and offline snapshot compatibility; no performance or production replay acceptance.', 'stages': []}

    def record(stage, **fields):
        result['stages'].append({'stage': stage, **fields})
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result['stages'][-1]), flush=True)

    cases = [('Conflict.cache.test', 1), ('CONFLICT.cache.test', 1),
             ('Existing.cache.test', 1), ('existing.cache.test', 1),
             ('MiXeD.cache.test', 1), ('MiXeD.cache.test', 28), ('TxT.cache.test', 16),
             ('cname.MiXeD.cache.test', 1), ('partial.MiXeD.cache.test', 1),
             ('missing.MiXeD.cache.test', 1), ('nodata.MiXeD.cache.test', 28),
             ('short.MiXeD.cache.test', 1), ('Signed.secure.cache.test', 1), ('Expired.secure.cache.test', 1)]

    def validate(answer, name, kind, address):
        lower = name.lower()
        if lower.startswith('bogus.'):
            assert answer['rcode'] == 2, answer
        elif lower.startswith('missing.') or lower.startswith('nodata.'):
            assert answer['rcode'] == (3 if lower.startswith('missing.') else 0) and not answer['answers'], answer
        else:
            expected = address if kind == 1 else '2001:db8::10' if kind == 28 else b'\x07fixture'.hex()
            assert answer['rcode'] == 0 and any(a['type'] == kind and a['value'] == expected for a in answer['answers']), answer
            if lower.endswith('secure.cache.test'):
                assert answer['authenticated'], answer
        return answer

    try:
        with lab(args.work / 'seed', None, old, fixtures) as active:
            for db, fixture in enumerate(fixtures):
                for name, kind in cases:
                    validate(ask(active.ports[db], name, kind, dnssec=True), name, kind, fixture.address)
                    if name.lower() in ('conflict.cache.test', 'existing.cache.test'):
                        active.restart_unbounds()
                validate(ask(active.ports[db], 'bogus.secure.cache.test', dnssec=True), 'bogus.secure.cache.test', 1, fixture.address)
                with_client = Redis(active.socket)
                try:
                    with_client.call('SELECT', db)
                    # Make a real old-format conflict, with explicit different answer bytes.
                    q = question('CONFLICT.cache.test', identifier=0)[12:]
                    key = lookup_key(q, SEED)
                    raw = with_client.call('GET', key)
                    assert raw is not None
                    parse_value(raw)
                    _, question_end = read_name(raw, 12)
                    _, first_rr = read_name(raw, question_end + 4)
                    assert struct.unpack('!HHIH', raw[first_rr:first_rr + 10])[:2] == (1, 1)
                    data_start = first_rr + 10
                    changed = raw[:data_start] + socket.inet_aton('192.0.2.99') + raw[data_start + 4:]
                    assert parse_value(changed)['question'] == q
                    with_client.call('SET', key, changed)
                    # Make a CNAME-only intermediate item, preserving its old time trailer.
                    key = lookup_key(question('partial.MiXeD.cache.test', identifier=0)[12:], SEED)
                    raw = with_client.call('GET', key)
                    parsed_name, offset = read_name(raw, 12)
                    _, rr_offset = read_name(raw, offset + 4)
                    size = struct.unpack('!HHIH', raw[rr_offset:rr_offset + 10])[-1]
                    head = raw[:6] + b'\0\1\0\0\0\0'
                    partial = head + raw[12:rr_offset + 10 + size] + raw[-16:]
                    parse_value(partial)
                    with_client.call('SET', key, partial)
                    # Absolute Redis expiry must be copied without re-starting its TTL.
                    key = lookup_key(question('MiXeD.cache.test', identifier=0)[12:], SEED)
                    with_client.call('PEXPIREAT', key, int(time.time() * 1000) + 120000)
                    with_client.call('SET', 'user-string', b'\0opaque\xff')
                    with_client.call('HSET', 'user-hash', 'field', 'preserve')
                    with_client.call('SET', b'A' * 64, b'unsupported DNS blob')
                    raw = with_client.call('GET', lookup_key(question('TxT.cache.test', 16, identifier=0)[12:], SEED))
                    with_client.call('SET', lookup_key(parse_value(raw)['question'], b'wrong-seed'), raw)
                finally:
                    with_client.close()
            assert active.cli('SAVE') == 'OK'
        original = args.work / 'original.rdb'
        shutil.copyfile(args.work / 'seed/cache.rdb', original)
        original_hash = file_hash(original)
        record('old_seed', rdb_sha256=original_hash, queries_per_db=len(cases) + 1, valid_dnssec=True, bogus_servfail=True)

        seeds = args.work / 'seeds.json'
        seeds.write_text(json.dumps({'0': SEED.decode(), '1': SEED.decode()}))
        command = [sys.executable, str(ROOT / 'scripts/prepare-cachedb-upgrade.py'), '--input-rdb', str(original),
                   '--bundle', str(args.bundle.resolve()), '--seeds-file', str(seeds)]
        planned = args.work / 'planned'
        subprocess.run([*command, '--work', str(planned)], check=True, timeout=150)
        plan_path = planned / 'plan.json'
        plan = json.loads(plan_path.read_text())
        assert len(plan['conflicts']) == 2, plan['counts']
        assert plan['counts']['existing_target_groups_preserved'] >= 2
        assert plan['counts']['unsupported_value'] == 2 and plan['counts']['seed_or_key_mismatch'] >= 2
        prepared = args.work / 'prepared'
        subprocess.run([*command, '--work', str(prepared), '--apply-plan', str(plan_path),
                        '--expected-plan-sha256', file_hash(plan_path)], check=True, timeout=150)
        prepared_result = json.loads((prepared / 'result.json').read_text())
        assert prepared_result['passed'] and prepared_result['input_unchanged']
        assert prepared_result['applied']['aliases_added'] == len(plan['aliases'])
        record('offline_prepare', aliases=len(plan['aliases']), conflicts=len(plan['conflicts']),
               counts=plan['counts'], categories=plan['categories'], input_unchanged=file_hash(original) == original_hash)

        # Verify every retained byte/type/deadline before any DNS query can refresh it.
        from cachedb_case_compat import disposable_redis
        with disposable_redis(original, args.work / 'audit-original', new['redis-server']) as (client, _):
            before = contents(client)
        with disposable_redis(prepared / 'cache.rdb', args.work / 'audit-prepared', new['redis-server']) as (client, _):
            after = contents(client)
        for db in (0, 1):
            assert all(after[db].get(key) == value for key, value in before[db].items())
            assert len(after[db]) - len(before[db]) == sum(row['db'] == db for row in plan['aliases'])
            for row in plan['aliases']:
                if row['db'] == db:
                    assert after[db][row['target_key'].encode()] == before[db][row['source_key'].encode()]
        record('all_original_keys_preserved', keys_by_db={db: len(rows) for db, rows in before.items()}, byte_type_expiry_equal=True)

        # Expire the DNS TTLs and a once-valid signature, preserving Redis deadlines.
        time.sleep(max(2.1, max(f.short_expiration for f in fixtures) - time.time() + 1.1))
        for label, snapshot, commands in [('candidate-reload', prepared / 'cache.rdb', new),
                                            ('old-reading-prepared', prepared / 'cache.rdb', old),
                                            ('rollback-original', original, old)]:
            with lab(args.work / label, snapshot, commands, fixtures) as active:
                observed = []
                for db, fixture in enumerate(fixtures):
                    for name, kind in cases:
                        if name in ('Conflict.cache.test', 'CONFLICT.cache.test'):
                            continue  # Deliberate historical conflict is separately checked for no alias.
                        start = fixture.requests()
                        question_before = fixture.question_snapshot()
                        client = Redis(active.socket)
                        try:
                            client.call('SELECT', db)
                            stats_before = client.call('INFO', 'stats')
                            answer = ask(active.ports[db], name, kind, tcp=db == 1, dnssec=True)
                            if name.lower().startswith('expired.'):
                                assert answer['rcode'] == 2 and not answer['authenticated'], answer
                            elif name.startswith('partial.') and label != 'candidate-reload':
                                assert answer['rcode'] == 0 and len(answer['answers']) == 1 and answer['answers'][0]['type'] == 5, answer
                            else:
                                validate(answer, name, kind, fixture.address)
                            stats_after = client.call('INFO', 'stats')
                        finally:
                            client.close()
                        hit = lambda raw: int(dict(line.split(b':', 1) for line in raw.splitlines() if b':' in line)[b'keyspace_hits'])
                        row = {'db': db, 'name': name, 'type': kind, 'upstream_delta': fixture.requests() - start,
                               'redis_hits': hit(stats_after) - hit(stats_before), 'authenticated': answer['authenticated'],
                               'ttls': [a['ttl'] for a in answer['answers']]}
                        question_delta = fixture.question_snapshot() - question_before
                        row['upstream_questions'] = [[*key, count] for key, count in sorted(question_delta.items())]
                        if label == 'candidate-reload' and name in ('MiXeD.cache.test', 'TxT.cache.test'):
                            assert row['redis_hits'] >= 1 and row['upstream_delta'] == 0, row
                        if label == 'candidate-reload' and name == 'Signed.secure.cache.test':
                            assert row['redis_hits'] >= 1 and all(key[:3] == ('secure.cache.test', 48, 1) for key in question_delta), row
                        if label == 'candidate-reload' and name.startswith('partial.'):
                            assert row['upstream_delta'] >= 1, row
                        if name.startswith('short.'):
                            assert max(row['ttls']) <= 2, row
                        observed.append(row)
                    validate(ask(active.ports[db], 'bogus.secure.cache.test', dnssec=True), 'bogus.secure.cache.test', 1, fixture.address)
                if label == 'candidate-reload':
                    for db, fixture in enumerate(fixtures):
                        for name, kind in [('mixed.cache.test', 1), ('MIXED.CACHE.TEST', 28), ('TXT.CACHE.TEST', 16)]:
                            active.restart_unbounds()
                            before = fixture.requests()
                            client = Redis(active.socket)
                            try:
                                client.call('SELECT', db)
                                initial = client.call('INFO', 'stats')
                                answer = validate(ask(active.ports[db], name, kind, tcp=db == 1, dnssec=True), name, kind, fixture.address)
                                final = client.call('INFO', 'stats')
                            finally:
                                client.close()
                            row = {'db': db, 'name': name, 'type': kind, 'upstream_delta': fixture.requests() - before,
                                   'redis_hits': hit(final) - hit(initial), 'ttls': [a['ttl'] for a in answer['answers']]}
                            assert row['redis_hits'] == 1 and row['upstream_delta'] == 0, row
                            observed.append(row)
                assert active.cli('SAVE') == 'OK'
                record(label, queries=observed, bogus_servfail=True)
        assert file_hash(original) == original_hash
        result.update(passed=True, original_unchanged=True,
                      limits=['Controlled records are not a replay of the historical production cache.',
                              'Conflicting case groups intentionally receive no alias and require natural refresh.',
                              'Signature and CNAME validation are real; no general performance conclusion is made.'])
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'passed': True, 'stages': len(result['stages'])}), flush=True)
    finally:
        for fixture in fixtures:
            fixture.close()


if __name__ == '__main__':
    main()
