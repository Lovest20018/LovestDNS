"""Measure mixed-case cache rebuilding using only disposable RDB copies and DNS."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import time

from dns_lab import Fixture, candidate_command, query, running_lab

ROOT = Path(__file__).resolve().parents[1]


def stats(lab):
    values = dict(line.split(':', 1) for line in lab.cli('INFO', 'stats').splitlines() if ':' in line)
    return {key: int(values[key]) for key in ('keyspace_hits', 'keyspace_misses', 'evicted_keys')}


def dns_memory(lab):
    values = dict(line.split(':', 1) for line in lab.cli('INFO', 'memory').splitlines() if ':' in line)
    return {key: int(values[key]) for key in ('used_memory', 'used_memory_dataset', 'maxmemory')}


def names(count):
    return [f'{"MiXeD" if i % 2 else "lower"}-{i}.cache.test' for i in range(count)]


def read_all(lab, fixtures, questions):
    before = stats(lab)
    requests = [fixture.requests() for fixture in fixtures]
    began = time.monotonic()
    checked = 0
    for db, fixture in enumerate(fixtures):
        for name in questions:
            for qtype in (1, 28):
                result = query(lab.ports[db], name, qtype=qtype)
                expected = fixture.address if qtype == 1 else '2001:db8::10'
                assert result['rcode'] == 0 and len(result['answers']) == 1, result
                answer = result['answers'][0]
                assert answer['value'] == expected and answer['type'] == qtype and answer['class'] == 1, result
                assert 0 < answer['ttl'] <= 600, 'The rebuild probe must use fresh records'
                checked += 1
    after = stats(lab)
    return {'queries': checked, 'elapsed_seconds': time.monotonic() - began,
            'redis_delta': {key: after[key] - before[key] for key in before},
            'upstream_requests_by_db': [fixture.requests() - old for fixture, old in zip(fixtures, requests)],
            'entries_by_db': [int(lab.cli('DBSIZE', db=db)) for db in range(len(fixtures))],
            'redis_memory': dns_memory(lab)}


def require_path(row, *, hits, misses, upstream_by_db, entries_by_db):
    assert row['redis_delta'] == {'keyspace_hits': hits, 'keyspace_misses': misses, 'evicted_keys': 0}, row
    assert row['upstream_requests_by_db'] == upstream_by_db, row
    assert row['entries_by_db'] == entries_by_db, row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-home', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--names-per-db', type=int, default=200)
    args = parser.parse_args()
    if args.work.exists() or args.names_per_db % 2 or not 2 <= args.names_per_db <= 200:
        parser.error('Use a new disposable work directory and an even name count in 2..200')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    args.work.mkdir(parents=True)
    old = {name: [str((args.baseline_home / 'core' / name).resolve())]
           for name in ('redis-server', 'redis-cli', 'unbound')}
    new = {name: candidate_command(args.bundle, 'unbound' if name == 'unbound' else 'redis', name) for name in old}
    fixtures = [Fixture('192.0.2.10', ttl=600, threaded_udp=False),
                Fixture('192.0.2.20', ttl=600, threaded_udp=False)]
    questions = names(args.names_per_db)
    per_db = len(questions) * 2
    total = per_db * len(fixtures)
    mixed_total = total // 2
    result = {'passed': False, 'scope': 'Fresh synthetic cachedb rebuilding; not a production impact estimate or performance acceptance',
              'names_per_db': len(questions), 'dbs': [0, 1], 'ttl_seconds': 600,
              'artificial_mixed_case_fraction': .5, 'stages': [],
              'lock_sha256': hashlib.sha256((ROOT / 'packaging/components.lock.json').read_bytes()).hexdigest()}
    original = args.work / 'original.rdb'

    def record(stage, row):
        result['stages'].append({'stage': stage, **row})
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'stage': stage, **row}), flush=True)

    def lab_from_snapshot(label, snapshot, commands):
        work = args.work / label
        work.mkdir()
        shutil.copyfile(snapshot, work / 'cache.rdb')
        return running_lab(work, commands['redis-server'], commands['redis-cli'], commands['unbound'], fixtures)

    try:
        with running_lab(args.work / 'seed', old['redis-server'], old['redis-cli'], old['unbound'], fixtures) as lab:
            row = read_all(lab, fixtures, questions)
            require_path(row, hits=0, misses=total, upstream_by_db=[per_db] * 2, entries_by_db=[per_db] * 2)
            assert lab.cli('SAVE') == 'OK'
            record('old_seed', row)
        shutil.copyfile(args.work / 'seed/cache.rdb', original)
        result['original_sha256'] = hashlib.sha256(original.read_bytes()).hexdigest()

        with lab_from_snapshot('candidate', original, new) as lab:
            row = read_all(lab, fixtures, questions)
            require_path(row, hits=total - mixed_total, misses=mixed_total,
                         upstream_by_db=[per_db // 2] * 2, entries_by_db=[per_db * 3 // 2] * 2)
            record('candidate_first_read', row)
            lab.restart_unbounds()
            row = read_all(lab, fixtures, questions)
            require_path(row, hits=total, misses=0, upstream_by_db=[0, 0], entries_by_db=[per_db * 3 // 2] * 2)
            assert lab.cli('SAVE') == 'OK'
            record('candidate_after_unbound_restart', row)
        updated = args.work / 'candidate.rdb'
        shutil.copyfile(args.work / 'candidate/cache.rdb', updated)
        result['candidate_rdb_sha256'] = hashlib.sha256(updated.read_bytes()).hexdigest()

        with lab_from_snapshot('candidate-reload', updated, new) as lab:
            row = read_all(lab, fixtures, questions)
            require_path(row, hits=total, misses=0, upstream_by_db=[0, 0], entries_by_db=[per_db * 3 // 2] * 2)
            record('candidate_after_rdb_reload', row)
        for label, snapshot, entries in [('rollback-original', original, per_db),
                                         ('old-reading-candidate', updated, per_db * 3 // 2)]:
            with lab_from_snapshot(label, snapshot, old) as lab:
                row = read_all(lab, fixtures, questions)
                require_path(row, hits=total, misses=0, upstream_by_db=[0, 0], entries_by_db=[entries] * 2)
                record(label, row)
        assert hashlib.sha256(original.read_bytes()).hexdigest() == result['original_sha256']
        result.update(passed=True, original_unchanged=True,
                      limitations=['Half the names are deliberately mixed case; this is not the real-world affected fraction.',
                                   'The extra old keys remain with redis-expire-records disabled; zero eviction was required here.',
                                   'Rollback acceptance uses the original snapshot; reading the candidate snapshot is an additional isolated check.',
                                   'Read elapsed times include sequential test-client overhead and are not DNS performance measurements.'])
        (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    finally:
        for fixture in fixtures:
            fixture.close()


if __name__ == '__main__':
    main()
