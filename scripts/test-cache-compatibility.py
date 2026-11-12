"""Verify real cachedb reuse across old/new DNS components using disposable data."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import time

from dns_lab import Fixture, candidate_command, query, running_lab

ROOT = Path(__file__).resolve().parents[1]


def check(result, address, *, max_ttl=120):
    assert result['rcode'] == 0 and result['answers'], result
    assert result['answers'][0]['value'] == address, result
    assert 0 <= result['answers'][0]['ttl'] <= max_ttl, result
    return result['answers'][0]['ttl']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-home', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True, help='New disposable directory, never a live data directory')
    args = parser.parse_args()
    if args.work.exists():
        parser.error('Work directory must not exist')
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    args.work.mkdir(parents=True)
    old = {name: [str((args.baseline_home / 'core' / name).resolve())]
           for name in ('redis-server', 'redis-cli', 'unbound')}
    new = {name: candidate_command(args.bundle, 'unbound' if name == 'unbound' else 'redis', name) for name in old}
    fixtures = [Fixture('192.0.2.10', ttl=600), Fixture('192.0.2.20', ttl=600)]
    results = []
    try:
        with running_lab(args.work / 'seed', old['redis-server'], old['redis-cli'], old['unbound'], fixtures) as lab:
            for db, fixture in enumerate(fixtures):
                check(query(lab.ports[db], 'retained.cache.test'), fixture.address, max_ttl=600)
                check(query(lab.ports[db], 'short.cache.test'), fixture.address, max_ttl=2)
                assert int(lab.cli('DBSIZE', db=db)) >= 2
            assert lab.cli('SAVE') == 'OK'
        snapshot = args.work / 'original.rdb'
        shutil.copyfile(args.work / 'seed/cache.rdb', snapshot)
        original = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        # Unbound stores expiry at integer-second resolution. Move strictly
        # beyond the 2s lifetime including its boundary second before probing.
        time.sleep(3.1)
        for label, redis_new, unbound_new in [('baseline', False, False), ('redis-only', True, False),
                                             ('unbound-only', False, True), ('combined', True, True),
                                             ('rollback-original', False, False)]:
            work = args.work / label
            work.mkdir()
            shutil.copyfile(snapshot, work / 'cache.rdb')
            redis, unbound = (new if redis_new else old), (new if unbound_new else old)
            with running_lab(work, redis['redis-server'], redis['redis-cli'], unbound['unbound'], fixtures) as lab:
                keys = [int(lab.cli('DBSIZE', db=db)) for db in (0, 1)]
                before = [f.requests() for f in fixtures]
                ttls = [check(query(lab.ports[db], 'retained.cache.test'), fixture.address, max_ttl=599)
                        for db, fixture in enumerate(fixtures)]
                assert [f.requests() for f in fixtures] == before, 'Old RDB was loaded but not reused'
                stats = lab.cli('INFO', 'stats')
                hits = int(next(line.split(':')[1] for line in stats.splitlines() if line.startswith('keyspace_hits:')))
                assert hits >= 2, 'No Redis reads for retained answers'
                for db, fixture in enumerate(fixtures):
                    # Same seed and qname, different DBs and answers: detect cross-use.
                    check(query(lab.ports[db], 'fresh-' + label + '.cache.test'), fixture.address, max_ttl=600)
                    short_before = fixture.counts[('short.cache.test', 'udp')]
                    check(query(lab.ports[db], 'short.cache.test'), fixture.address, max_ttl=2)
                    deadline = time.monotonic() + 3
                    while fixture.counts[('short.cache.test', 'udp')] == short_before and time.monotonic() < deadline:
                        time.sleep(.02)
                    assert fixture.counts[('short.cache.test', 'udp')] > short_before, 'Expired entry was not refreshed'
                    check(query(lab.ports[db], 'trunc.cache.test'), fixture.address, max_ttl=600)
                    assert fixture.counts[('trunc.cache.test', 'tcp')] > 0
                    assert query(lab.ports[db], 'missing.cache.test')['rcode'] == 3
                    alias = query(lab.ports[db], 'cname.cache.test')['answers']
                    assert [item['value'] for item in alias] == ['target.cache.test', fixture.address], alias
                assert lab.cli('SAVE') == 'OK'
                lab.restart_all()
                before = [f.requests() for f in fixtures]
                for db, fixture in enumerate(fixtures):
                    check(query(lab.ports[db], 'fresh-' + label + '.cache.test'), fixture.address, max_ttl=600)
                assert [f.requests() for f in fixtures] == before, 'Newly written entries not reused'
                result = {'scenario': label, 'old_rdb_loaded': True, 'old_entries_reused': True,
                          'db_keys': keys, 'remaining_ttls': ttls, 'redis_keyspace_hits': hits,
                          'db_isolation': True, 'new_entries_reused_after_rdb_reload': True,
                          'expired_entries_refreshed': True, 'tcp_fallback': True}
                results.append(result)
                print(json.dumps(result), flush=True)
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == original
        (args.work / 'result.json').write_text(json.dumps({'passed': True, 'original_sha256': original,
                                                         'scenarios': results}, indent=2) + '\n')
    finally:
        for fixture in fixtures:
            fixture.close()


if __name__ == '__main__':
    main()
