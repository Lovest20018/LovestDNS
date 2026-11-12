"""Plan or prepare additive lowercase cachedb aliases on an offline RDB copy."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import time

from cachedb_case_compat import (apply_plan, build_plan, canonical_json, disposable_redis,
                               file_hash)
from dns_lab import candidate_command

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-rdb', type=Path, required=True, help='Immutable completed snapshot, never a live Redis connection')
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--seeds-file', type=Path, required=True, help='Private JSON object mapping DB 0 and 1 to their exact cachedb seeds')
    parser.add_argument('--work', type=Path, required=True, help='Fresh private output directory')
    parser.add_argument('--apply-plan', type=Path, help='Previously produced plan.json; omitted means read-only planning')
    parser.add_argument('--expected-plan-sha256', help='Required when applying a reviewed plan')
    args = parser.parse_args()
    if args.work.exists() or not args.input_rdb.is_file():
        parser.error('Use an existing immutable input RDB and a new work directory')
    if bool(args.apply_plan) != bool(args.expected_plan_sha256):
        parser.error('Applying requires both the plan path and its expected SHA-256')
    private_seeds = json.loads(args.seeds_file.read_text())
    if set(private_seeds) != {'0', '1'} or not all(isinstance(v, str) for v in private_seeds.values()):
        parser.error('The seeds file must explicitly name DB 0 and DB 1')
    seeds = {int(db): value.encode() for db, value in private_seeds.items()}
    lock = json.loads((ROOT / 'packaging/components.lock.json').read_text())
    runpy.run_path(str(ROOT / 'packaging/components/bundle.py'))['verify'](args.bundle, lock)
    inputs = {'input_rdb_sha256': file_hash(args.input_rdb),
              'bundle_manifest_sha256': file_hash(args.bundle / 'manifest.json'),
              'seed_sha256': {str(db): hashlib.sha256(seed).hexdigest() for db, seed in seeds.items()}}
    plan = None
    if args.apply_plan:
        if file_hash(args.apply_plan) != args.expected_plan_sha256:
            raise ValueError('Reviewed plan file SHA-256 mismatch')
        plan = json.loads(args.apply_plan.read_text())
        if any(plan.get(key) != value for key, value in inputs.items()):
            raise ValueError('Reviewed plan does not describe these exact inputs')
    result = {'passed': False, 'mode': 'prepare' if plan else 'plan', 'recorded_at': time.time(), **inputs,
              'scope': 'Offline additive lookup-key compatibility; not approval of a component upgrade or DNS performance.'}
    with disposable_redis(args.input_rdb, args.work,
                          candidate_command(args.bundle, 'redis', 'redis-server')) as (client, copied):
        if plan is None:
            plan = {**build_plan(client, seeds), **inputs}
        else:
            # Re-derive the aliases instead of trusting arbitrary keys from a
            # supplied plan, then tolerate only natural Redis expiry of sources.
            refreshed = {**build_plan(client, seeds, **plan['limits']), **inputs}
            expected = {(r['db'], r['source_key'], r['target_key']): r for r in plan['aliases']}
            current = {(r['db'], r['source_key'], r['target_key']): r for r in refreshed['aliases']}
            if any(expected.get(key) != row for key, row in current.items()):
                raise ValueError('Re-derived aliases differ from the reviewed plan')
            for key in expected.keys() - current.keys():
                expire_at = expected[key]['redis_expire_at_ms']
                if expire_at < 0 or expire_at > time.time() * 1000:
                    raise ValueError('A non-expired reviewed alias disappeared')
            result['applied'] = apply_plan(client, plan)
            # SAVE writes a new file and renames it. The immutable input remains
            # available, so release only this process's disposable load copy to
            # avoid holding a third full-sized RDB during the rewrite.
            copied.unlink()
            if client.call('SAVE') != b'OK':
                raise ValueError('Prepared snapshot was not saved')
            result['output_rdb_sha256'] = file_hash(copied)
            result['output_rdb_bytes'] = copied.stat().st_size
            result['scratch_input_released_before_save'] = True
        plan_path = args.work / 'plan.json'
        plan_path.write_bytes(canonical_json(plan) + b'\n')
        result.update(passed=True, plan_sha256=file_hash(plan_path),
                      planned_aliases=len(plan['aliases']), unresolved_conflicting_groups=len(plan['conflicts']),
                      counts=plan['counts'], categories=plan['categories'])
    result['input_unchanged'] = file_hash(args.input_rdb) == inputs['input_rdb_sha256']
    if not result['input_unchanged']:
        raise ValueError('Input snapshot changed')
    (args.work / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
