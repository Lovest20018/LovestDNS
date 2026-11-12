"""Replay the complete derivable lookup set of a private prepared cachedb copy.

Historical TTLs stay unchanged. Both ordinary Unbound versions use explicit
immediate stale serving, no prefetch, no cachedb writes, and a loopback SERVFAIL
upstream. This checks retained-answer compatibility, not fresh-data validation
or performance. The catalog and all results are private cache evidence.
"""
import argparse
from collections import Counter
import contextlib
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import struct
import subprocess
import tempfile
import time

from cachedb_case_compat import KEY, Redis, RedisCommandError, file_hash, lookup_key, parse_value, wire_name
from dns_lab import Child, Fixture, candidate_command, free_port

ROOT = Path(__file__).resolve().parents[1]


def info(client, section):
    return {key.decode(): value.decode() for line in client.call('INFO', section).splitlines()
            if b':' in line for key, value in [line.split(b':', 1)]}


def catalog(client, seeds, destination, progress):
    counts = Counter()
    with destination.open('xb') as stream:
        for db, seed in sorted(seeds.items()):
            client.call('SELECT', db)
            cursor, seen, questions = b'0', set(), set()
            while True:
                cursor, keys = client.call('SCAN', cursor, 'COUNT', 512)
                unique = []
                for key in keys:
                    if key in seen:
                        counts['scan_duplicates'] += 1
                    else:
                        seen.add(key)
                        unique.append(key)
                values = client.call('MGET', *unique) if unique else []
                for key, value in zip(unique, values):
                    counts['keys'] += 1
                    if value == b'\0' * 4:
                        counts['removal_markers'] += 1
                        continue
                    if value is None or not KEY.fullmatch(key):
                        raise ValueError('Unexpected non-cachedb data; classify before replay')
                    entry = parse_value(value)
                    old, new = (lookup_key(entry[field], seed) for field in ('question', 'lower_question'))
                    if key not in (old, new):
                        counts['unverifiable_key_preserved'] += 1
                        continue
                    if new in questions:
                        counts['duplicate_canonical_lookup'] += 1
                        continue
                    questions.add(new)
                    target = value if key == new else client.call('GET', new)
                    kind = 1
                    if target is None or target == b'\0' * 4:
                        kind = 2
                        counts['missing_canonical_target'] += 1
                    else:
                        target_entry = parse_value(target)
                        if target_entry['lower_question'] != entry['lower_question']:
                            raise ValueError('A derived lookup target contains another question')
                        value = target
                    frame = bytes([db, kind]) + value
                    stream.write(struct.pack('!I', len(frame)) + frame)
                    counts['catalog_records'] += 1
                    counts['catalog_db_' + str(db)] += 1
                if counts['keys'] > 3000000:
                    raise ValueError('Three-million-key catalog budget exceeded')
                if counts['keys'] % 20000 < 600:
                    progress(dict(counts))
                if cursor == b'0':
                    break
    return dict(counts)


@contextlib.contextmanager
def isolated_redis(snapshot, work, command, memory):
    work.mkdir(mode=0o700)
    os.link(snapshot, work / 'cache.rdb')
    with tempfile.TemporaryDirectory(prefix='ldns-history-') as temp:
        path = Path(temp) / 'r.sock'
        child = Child([*command, '--port', '0', '--unixsocket', str(path), '--unixsocketperm', '700',
                       '--dir', str(work), '--dbfilename', 'cache.rdb', '--save', '', '--appendonly', 'no',
                       '--maxmemory', str(memory), '--maxmemory-policy', 'allkeys-lru'], work / 'redis.log')
        client = None
        try:
            deadline = time.monotonic() + 120
            while client is None:
                child.check()
                try:
                    client = Redis(path)
                    assert client.call('PING') == b'PONG'
                except (OSError, RedisCommandError) as error:
                    if isinstance(error, RedisCommandError) and not error.response.startswith(b'LOADING '):
                        raise
                    if client:
                        client.close()
                        client = None
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Historical Redis readiness deadline')
                    time.sleep(.1)
            yield client, path
        finally:
            if client:
                client.close()
            child.close()


def configuration(directory, port, db, seed, redis_path, fixture_port):
    return f'''server:
    interface: 127.0.0.1
    port: {port}
    username: ""
    chroot: ""
    directory: "{directory}"
    pidfile: "{directory}/u.pid"
    use-syslog: no
    verbosity: 0
    do-ip6: no
    do-not-query-localhost: no
    local-zone: "test." transparent
    num-threads: 1
    module-config: "cachedb iterator"
    msg-cache-size: 4m
    rrset-cache-size: 8m
    cache-max-ttl: 600
    prefetch: no
    serve-expired: yes
    serve-expired-ttl: 0
    serve-expired-client-timeout: 0
    serve-expired-reply-ttl: 0
cachedb:
    backend: "redis"
    secret-seed: "{seed}"
    redis-server-path: "{redis_path}"
    redis-logical-db: {db}
    redis-timeout: 100
    redis-expire-records: no
    cachedb-check-when-serve-expired: no
    cachedb-no-store: yes
forward-zone:
    name: "."
    forward-addr: 127.0.0.1@{fixture_port}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'baseline-home', 'bundle', 'helper', 'seeds-file', 'work'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--expected-snapshot-sha256', required=True)
    parser.add_argument('--expected-helper-sha256', required=True)
    parser.add_argument('--redis-memory', required=True, type=int)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--transport', choices=('udp', 'tcp'), default='udp')
    args = parser.parse_args()
    if args.work.exists() or not 1 <= args.workers <= 32 or args.redis_memory < 64 * 1024 ** 2:
        parser.error('New work directory, bounded workers and positive Redis budget required')
    if Path('/proc/self/ns/net').stat().st_ino == Path('/proc/1/ns/net').stat().st_ino:
        parser.error('Run inside an independent loopback-only network namespace')
    assert file_hash(args.snapshot) == args.expected_snapshot_sha256
    assert file_hash(args.helper) == args.expected_helper_sha256
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    private = json.loads(args.seeds_file.read_text())
    if set(private) != {'0', '1'} or any(not isinstance(v, str) or not v or any(c in v for c in '\n\r"\\') for v in private.values()):
        parser.error('Two explicit config-safe private seed strings required')
    seeds = {int(k): v.encode() for k, v in private.items()}
    args.work.mkdir(mode=0o700)
    started = time.monotonic()
    result = {'passed': False, 'status': 'running', 'snapshot_sha256': args.expected_snapshot_sha256,
              'helper_sha256': args.expected_helper_sha256,
              'redis_maxmemory': args.redis_memory, 'redis_policy': 'allkeys-lru',
              'transport': args.transport,
              'scope': 'All derivable canonical lookups from an immutable prepared historical snapshot; explicit immediate-stale/no-write policy; no DNSSEC validation or performance approval.'}
    def save():
        temp = args.work / 'result.tmp'
        temp.write_text(json.dumps(result, indent=2) + '\n')
        temp.replace(args.work / 'result.json')
    def progress(counts):
        result['catalog_progress'] = counts
        save()
    children, fixtures = [], []
    try:
        subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
        with isolated_redis(args.snapshot, args.work / 'redis', candidate_command(args.bundle, 'redis', 'redis-server'), args.redis_memory) as (client, redis_path):
            result['memory_loaded'] = info(client, 'memory')
            data = args.work / 'private-catalog.bin'
            result['catalog'] = catalog(client, seeds, data, progress)
            result['catalog_sha256'] = file_hash(data)
            result['catalog_bytes'] = data.stat().st_size
            before = info(client, 'stats')
            assert before['evicted_keys'] == '0'
            result['memory_catalogued'] = info(client, 'memory')
            save()
            for db in (0, 1):
                fixture = Fixture(threaded_udp=False)
                def respond(packet, tcp, fixture=fixture):
                    with fixture.lock:
                        fixture.counts[('all', 'tcp' if tcp else 'udp')] += 1
                    _, end = wire_name(packet, 12, compressed=False)
                    return packet[:2] + struct.pack('!5H', 0x8182, 1, 0, 0, 0) + packet[12:end + 4]
                fixture.respond = respond
                fixtures.append(fixture)
            ports = {'old': [], 'new': []}
            for version, command in [('old', [str((args.baseline_home / 'core/unbound').resolve())]),
                                     ('new', candidate_command(args.bundle, 'unbound', 'unbound'))]:
                for db in (0, 1):
                    directory = args.work / (version + '-' + str(db))
                    directory.mkdir(mode=0o700)
                    port = free_port()
                    ports[version].append(port)
                    config = directory / 'unbound.conf'
                    config.write_text(configuration(directory, port, db, private[str(db)], redis_path, fixtures[db].port))
                    child = Child([*command, '-d', '-p', '-c', str(config)], directory / 'unbound.log')
                    children.append(child)
                    deadline = time.monotonic() + 15
                    while True:
                        child.check()
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('Historical Unbound readiness deadline')
                            time.sleep(.05)
            result['ports'] = ports
            save()
            command = [str(args.helper), '--input', str(data), '--output', str(args.work / 'replay.json'),
                       '--old', ','.join('127.0.0.1:' + str(p) for p in ports['old']),
                       '--new', ','.join('127.0.0.1:' + str(p) for p in ports['new']), '--workers', str(args.workers),
                       '--transport', args.transport]
            with (args.work / 'replay.log').open('xb') as stream:
                completed = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=7300)
            replay = json.loads((args.work / 'replay.json').read_text())
            result['replay'] = replay
            result['component_returncodes_before_cleanup'] = [child.process.poll() for child in children]
            result['stats_after'] = info(client, 'stats')
            result['memory_after'] = info(client, 'memory')
            assert result['stats_after']['evicted_keys'] == '0'
            for db in (0, 1):
                client.call('SELECT', db)
                result['db_' + str(db) + '_keys_after'] = client.call('DBSIZE')
            result['upstream_by_db'] = [f.requests() for f in fixtures]
            save()
            assert all(code is None for code in result['component_returncodes_before_cleanup']), 'A replay component exited before cleanup'
            assert completed.returncode == 0 and replay['passed'], 'Old/new historical response compatibility failed; retained private result'
            assert replay['input_records'] == result['catalog']['catalog_records']
        result.update(passed=True, status='complete', elapsed=time.monotonic() - started)
    except BaseException as error:
        result.update(status='failed', error=repr(error))
        raise
    finally:
        result['component_returncodes_before_cleanup'] = [child.process.poll() for child in children]
        for child in reversed(children):
            child.close()
        for fixture in fixtures:
            fixture.close()
        result['snapshot_unchanged'] = file_hash(args.snapshot) == args.expected_snapshot_sha256
        save()
    print(json.dumps({'passed': result['passed'], 'elapsed': result.get('elapsed')}))


if __name__ == '__main__':
    main()
