"""Protect old cachedb records from alias conflicts, TTL resets and bad packets."""
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachedb_case_compat import Redis, RedisCommandError, apply_plan, build_plan, lookup_key, parse_value, wire_name

SEED = b'compatibility-test-seed'


def test_redis_loading_is_identifiable_without_suppressing_other_errors():
    client = Redis.__new__(Redis)
    for response in (b'LOADING Redis is loading the dataset in memory', b'ERR invalid configuration'):
        client.stream = io.BytesIO(b'-' + response + b'\r\n')
        with pytest.raises(RedisCommandError) as error:
            client._read()
        assert error.value.response == response


def value(name=b'\x05MiXeD\x05cache\x04test\0', *, address=b'\xc0\x00\x02\x0a', kind=1,
          timestamp=1700000000, expiry=1700000600, flags=0x8180, record_type=None):
    answer = b'\xc0\x0c' + struct.pack('!HHIH', record_type or kind, 1, 600, len(address)) + address
    return (struct.pack('!6H', 0, flags, 1, 1, 0, 0) + name + struct.pack('!HH', kind, 1)
            + answer + struct.pack('!QQ', timestamp, expiry))


def old_key(raw):
    return lookup_key(parse_value(raw)['question'], SEED)


def new_key(raw):
    return lookup_key(parse_value(raw)['lower_question'], SEED)


class Snapshot:
    def __init__(self, values, expires=None):
        self.values = {db: dict(rows) for db, rows in values.items()}
        self.expires = expires or {}
        self.db = 0
        self.writes = []

    def call(self, command, *args):
        if command == 'SELECT':
            self.db = int(args[0]); return b'OK'
        rows = self.values.get(self.db, {})
        if command == 'SCAN': return b'0', list(rows)
        if command == 'MGET': return [rows.get(key) for key in args]
        if command == 'GET': return rows.get(args[0])
        if command == 'EXISTS': return int(args[0] in rows)
        if command == 'PEXPIRETIME': return self.expires.get((self.db, args[0]), -1) if args[0] in rows else -2
        if command == 'EVAL':
            self.writes.append(args)
            raise AssertionError('Unexpected write during preflight rejection')
        raise AssertionError(command)


def test_alias_derivation_preserves_message_timestamps_and_db_isolation():
    first, second = value(), value(address=b'\xc0\x00\x02\x14')
    source = Snapshot({0: {old_key(first): first}, 1: {old_key(second): second}})
    original = deepcopy(source.values)
    plan = build_plan(source, {0: SEED, 1: SEED})
    assert len(plan['aliases']) == 2 and not plan['conflicts']
    assert {r['db'] for r in plan['aliases']} == {0, 1}
    for row, raw in zip(plan['aliases'], (first, second)):
        assert row['source_key'].encode() == old_key(raw)
        assert row['target_key'].encode() == new_key(raw)
        assert row['value_sha256'] == hashlib.sha256(raw).hexdigest()
        assert row['cachedb_timestamp'] == 1700000000 and row['cachedb_expiry'] == 1700000600
    assert source.values == original and not source.writes


def test_wire_labels_with_literal_dots_and_nonascii_bytes_are_not_text_split():
    name = b'\x05A.B\x80Z\x04test\0'
    raw = value(name)
    parsed = parse_value(raw)
    assert parsed['lower_question'] == b'\x05a.b\x80z\x04test\0\x00\x01\x00\x01'
    assert new_key(raw) != new_key(value(b'\x01A\x03B\x80Z\x04test\0'))


@pytest.mark.parametrize('changes', [
    {'address': b'\xc0\x00\x02\x63'}, {'timestamp': 1700000001},
    {'expiry': 1700000601}, {'flags': 0x81a0},
])
def test_conflicting_case_variants_never_choose_an_arbitrary_answer_or_time(changes):
    first, second = value(), value(b'\x05MIXED\x05cache\x04test\0', **changes)
    for rows in ({old_key(first): first, old_key(second): second},
                 {old_key(second): second, old_key(first): first}):
        plan = build_plan(Snapshot({0: rows}), {0: SEED})
        assert not plan['aliases'] and len(plan['conflicts']) == 1
        assert plan['counts']['conflicting_groups_preserved'] == 1


def test_equivalent_variants_require_identical_absolute_redis_expiry():
    a, b = value(), value(b'\x05MIXED\x05cache\x04test\0')
    rows = {old_key(a): a, old_key(b): b}
    result = build_plan(Snapshot({0: rows}), {0: SEED})
    assert len(result['aliases']) == 1 and len(result['aliases'][0]['equivalent_source_keys']) == 2
    expires = {(0, old_key(a)): 1800000000000, (0, old_key(b)): 1800000000001}
    assert not build_plan(Snapshot({0: rows}, expires), {0: SEED})['aliases']


def test_existing_lowercase_target_is_preserved_even_if_its_value_is_unknown():
    raw = value()
    source = Snapshot({0: {old_key(raw): raw, new_key(raw): b'unrecognized existing value'}})
    plan = build_plan(source, {0: SEED})
    assert not plan['aliases'] and plan['counts']['existing_target_groups_preserved'] == 1
    assert source.values[0][new_key(raw)] == b'unrecognized existing value'


def test_wrong_seed_and_unrecognized_keys_are_reported_without_copying():
    raw = value()
    result = build_plan(Snapshot({0: {old_key(raw): raw, b'user-key': b'user-value'}}), {0: b'wrong seed'})
    assert not result['aliases'] and result['counts']['seed_or_key_mismatch'] == 1
    assert result['counts']['non_cachedb_key'] == 1


@pytest.mark.parametrize('raw', [b'', b'x' * 32, value()[:-1], value() + b'x',
    value(timestamp=0), value(expiry=1699999999), value(flags=0x8380), value(flags=0x0180),
    value(flags=0x8182), value(b'\xc0\x0c'), value(b'\x40' + b'a' * 64 + b'\0')])
def test_malformed_or_unsupported_blobs_are_rejected(raw):
    with pytest.raises((ValueError, struct.error)):
        parse_value(raw)


def test_name_compression_cannot_escape_or_loop():
    for wire in (b'\xc0\x0c', b'\xc0\x00', b'\xc0\xff'):
        with pytest.raises(ValueError):
            wire_name(b'\0' * 12 + wire, 12, compressed=True)


@pytest.mark.parametrize('kind,rrtype,rcode', [(5, 5, 0), (28, 46, 0), (1, 6, 3), (16, 16, 0)])
def test_cname_negative_dnssec_and_other_types_stay_opaque(kind, rrtype, rcode):
    raw = value(kind=kind, record_type=rrtype, flags=0x8180 | rcode)
    result = build_plan(Snapshot({0: {old_key(raw): raw}}), {0: SEED})
    row = result['aliases'][0]
    assert row['value_sha256'] == hashlib.sha256(raw).hexdigest()
    assert row['bytes'] == len(raw)
    assert row['cachedb_expiry'] == 1700000600
    assert result['categories']['type_' + str(kind)] == 1
    assert result['categories']['rcode_' + str(rcode)] == 1


def test_cname_only_intermediate_is_preserved_without_creating_an_alias():
    raw = value(record_type=5)
    source = Snapshot({0: {old_key(raw): raw}})
    plan = build_plan(source, {0: SEED})
    assert not plan['aliases'] and plan['counts']['partial_cname_preserved'] == 1
    assert source.values[0][old_key(raw)] == raw and not source.writes


def test_partial_cname_prevents_aliasing_another_case_variant_in_its_group():
    partial = value(record_type=5)
    full = value(b'\x05MIXED\x05cache\x04test\0')
    plan = build_plan(Snapshot({0: {old_key(partial): partial, old_key(full): full}}), {0: SEED})
    assert not plan['aliases'] and plan['counts']['partial_cname_groups_preserved'] == 1


def test_skipped_partial_cname_records_still_count_against_the_scan_budget():
    raw = value(record_type=5)
    with pytest.raises(ValueError, match='budget'):
        build_plan(Snapshot({0: {old_key(raw): raw}}), {0: SEED}, max_aliases=0)


@pytest.mark.parametrize('change', ['source', 'expiry', 'target'])
def test_all_aliases_are_preflighted_before_any_copy(change):
    a, b = value(), value(b'\x05OtHeR\x05cache\x04test\0')
    source = Snapshot({0: {old_key(a): a, old_key(b): b}})
    plan = build_plan(source, {0: SEED})
    last = plan['aliases'][-1]
    key = last['source_key'].encode()
    if change == 'source': source.values[0][key] = value(address=b'\xc0\x00\x02\x63')
    elif change == 'expiry': source.expires[0, key] = 1800000000000
    else: source.values[0][last['target_key'].encode()] = b'new target'
    with pytest.raises(ValueError):
        apply_plan(source, plan)
    assert not source.writes


@pytest.mark.parametrize('budget', [{'max_keys': 0}, {'max_aliases': 0}, {'max_alias_bytes': 1}])
def test_plan_stops_on_declared_resource_budgets(budget):
    raw = value()
    with pytest.raises(ValueError, match='budget'):
        build_plan(Snapshot({0: {old_key(raw): raw}}), {0: SEED}, **budget)


def test_expired_dns_records_keep_their_past_timestamps_and_redis_deadline():
    raw = value(timestamp=1600000000, expiry=1600000600)
    deadline = 1800000000123
    result = build_plan(Snapshot({0: {old_key(raw): raw}}, {(0, old_key(raw)): deadline}), {0: SEED})
    row = result['aliases'][0]
    assert row['cachedb_expiry'] == 1600000600 and row['redis_expire_at_ms'] == deadline
