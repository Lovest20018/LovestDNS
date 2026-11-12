"""Verify old/new RDB writes, BGSAVE, reload and original-snapshot rollback."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import time

from cachedb_case_compat import canonical_json, disposable_redis, file_hash
from dns_lab import candidate_command, running_lab


def snapshot(client):
    """DUMP contains the exact binary value/type encoding; deadlines stay absolute."""
    digest, count, types, deadlines, volatile = hashlib.sha256(), 0, {}, {}, 0
    for db in (0, 1):
        client.call('SELECT', db)
        keys = set()
        cursor = b'0'
        while True:
            cursor, batch = client.call('SCAN', cursor, 'COUNT', 512)
            keys.update(batch)
            if cursor == b'0':
                break
        for key in sorted(keys):
            value = client.call('DUMP', key)
            expiry = client.call('PEXPIRETIME', key)
            assert value is not None and expiry != -2, 'Use deadlines long enough for this bounded comparison'
            kind = client.call('TYPE', key).decode()
            digest.update(canonical_json([db, key.hex(), value.hex(), expiry]) + b'\n')
            count += 1
            types[kind] = types.get(kind, 0) + 1
            if expiry >= 0:
                deadlines[f'{db}:{key.hex()}'] = expiry
                volatile += 1
    return {'sha256': digest.hexdigest(), 'keys': count, 'types': types,
            'absolute_deadlines': deadlines, 'volatile': volatile}


def info(client, section):
    return {k.decode(): v.decode() for line in client.call('INFO', section).splitlines()
            if b':' in line for k, v in [line.split(b':', 1)]}


def bgsave(client):
    previous = info(client, 'persistence')['rdb_saves']
    assert client.call('BGSAVE') == b'Background saving started'
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = info(client, 'persistence')
        if state['rdb_bgsave_in_progress'] == '0':
            assert state['rdb_last_bgsave_status'] == 'ok' and int(state['rdb_saves']) > int(previous)
            return {key: state[key] for key in ('rdb_last_bgsave_status', 'rdb_saves', 'rdb_last_cow_size', 'rdb_last_bgsave_time_sec')}
        time.sleep(.05)
    raise TimeoutError('Bounded BGSAVE did not finish')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline-home', 'bundle', 'work'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--keys-per-db', type=int, default=16000)
    args = parser.parse_args()
    if args.work.exists() or not 100 <= args.keys_per_db <= 20000:
        parser.error('Use a fresh work directory and 100..20000 keys per DB')
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, mode=0o700)
    root = Path(__file__).resolve().parents[1]
    runpy.run_path(str(root / 'packaging/components/bundle.py'))['verify'](
        args.bundle, json.loads((root / 'packaging/components.lock.json').read_text()))
    old = {name: [str((args.baseline_home / 'core' / name).resolve())]
           for name in ('redis-server', 'redis-cli', 'unbound')}
    new = candidate_command(args.bundle, 'redis', 'redis-server')
    result = {'passed': False, 'stages': [], 'scope': 'Controlled binary values, collection types and expiry through real RDB persistence; no DNS performance acceptance.'}

    def record(stage, **values):
        result['stages'].append({'stage': stage, **values})
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result['stages'][-1]), flush=True)

    # Create the initial empty RDB using the deployed baseline's own writer.
    with running_lab(args.work / 'empty', old['redis-server'], old['redis-cli'], old['unbound'], []) as active:
        assert active.cli('SAVE') == 'OK'
    initial = args.work / 'empty/cache.rdb'
    expected_deadline = int(time.time() * 1000) + 300000
    with disposable_redis(initial, args.work / 'old-seed', old['redis-server'], max_memory=256 * 1024 ** 2) as (client, original):
        for db in (0, 1):
            client.call('SELECT', db)
            for index in range(args.keys_per_db):
                block = hashlib.sha256(f'{db}:{index}'.encode()).digest() * 32
                client.call('SET', f'payload:{index:06d}', block)
            client.call('SET', b'\0binary-key\xff', b'\0binary-value\xff', 'PXAT', expected_deadline)
            client.call('HSET', 'hash', b'\0field', b'\xffvalue', 'counter', db)
            client.call('RPUSH', 'list', 'first', b'\0middle', 'last')
            client.call('SADD', 'set', 'one', b'\0two', 'three')
            client.call('ZADD', 'zset', '1.25', 'first', '-2.5', b'\0second')
            client.call('XADD', 'stream', '1-0', 'field', b'\0value')
            client.call('PEXPIREAT', 'hash', expected_deadline)
        expected = snapshot(client)
        assert info(client, 'stats')['evicted_keys'] == '0'
        assert client.call('SAVE') == b'OK'
        original_hash = file_hash(original)
        record('baseline_save', data=expected, rdb_sha256=original_hash, rdb_bytes=original.stat().st_size)
    immutable_original = args.work / 'original.rdb'
    shutil.copyfile(args.work / 'old-seed/cache.rdb', immutable_original)

    with disposable_redis(immutable_original, args.work / 'new-write', new, max_memory=256 * 1024 ** 2) as (client, rewritten):
        assert snapshot(client) == expected
        record('candidate_load_old', data=expected, evicted_keys=info(client, 'stats')['evicted_keys'])
        # Actually write with the new server, including overwrites and deletes.
        for db in (0, 1):
            client.call('SELECT', db)
            client.call('SET', 'candidate-added', b'\0new\xff' + bytes([db]))
            client.call('SET', 'payload:000000', b'updated-by-new-' + bytes([db]))
            client.call('DEL', 'payload:000001')
            client.call('HSET', 'hash', 'candidate-field', db + 10)
            client.call('SET', 'expires-now', 'temporary', 'PX', 100)
        time.sleep(.15)
        for db in (0, 1):
            client.call('SELECT', db)
            assert client.call('GET', 'expires-now') is None
        modified = snapshot(client)
        persistence = bgsave(client)
        assert snapshot(client) == modified
        assert info(client, 'stats')['evicted_keys'] == '0'
        rewritten_hash = file_hash(rewritten)
        record('candidate_write_bgsave', data=modified, persistence=persistence, rdb_sha256=rewritten_hash,
               rdb_bytes=rewritten.stat().st_size, naturally_expired_absent=True)
    saved = args.work / 'candidate.rdb'
    shutil.copyfile(args.work / 'new-write/cache.rdb', saved)

    for label, source, command, expected_state in [('candidate_reload', saved, new, modified),
                                                   ('old_reads_new_snapshot', saved, old['redis-server'], modified),
                                                   ('rollback_original', immutable_original, old['redis-server'], expected)]:
        with disposable_redis(source, args.work / label, command, max_memory=256 * 1024 ** 2) as (client, _):
            actual = snapshot(client)
            assert actual == expected_state, {'stage': label, 'actual': actual, 'expected': expected_state}
            assert info(client, 'stats')['evicted_keys'] == '0'
            record(label, data=actual, evicted_keys=0)
    assert file_hash(immutable_original) == original_hash
    assert file_hash(saved) == rewritten_hash
    result.update(passed=True, original_unchanged=True, candidate_unchanged_after_readback=True,
                  limits=['This bounded synthetic dataset is smaller than the historical production RDB.',
                          'AOF, disk-full/crash recovery and DNS query latency are separate checks.'])
    (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': True, 'stages': len(result['stages'])}), flush=True)


if __name__ == '__main__':
    main()
