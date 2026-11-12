"""Historical replay must retain the selected bytes and account for exclusions."""
from pathlib import Path
import runpy
import struct
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
replay = runpy.run_path(str(SCRIPTS / 'test-cachedb-history-replay.py'))
from cachedb_case_compat import lookup_key, parse_value
from dns_lab import question


def blob(name, address=b'\xc0\x00\x02\x01', *, lifetime=60):
    query = question(name, identifier=0)
    return (query[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + query[12:]
            + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, lifetime, 4) + address
            + struct.pack('!QQ', 1000, 1000 + lifetime))


def key(value, *, lower=False):
    entry = parse_value(value)
    return lookup_key(entry['lower_question' if lower else 'question'], b'synthetic-seed')


class CopiedRedis:
    def __init__(self, databases):
        self.databases, self.db = databases, 0

    def call(self, command, *args):
        data = self.databases[self.db]
        if command == 'SELECT':
            self.db = args[0]
        elif command == 'SCAN':
            return b'0', list(data)
        elif command == 'GET':
            return data.get(args[0])
        elif command == 'MGET':
            return [data.get(name) for name in args]
        else:
            raise AssertionError('Replay catalog attempted a write or another Redis command')


def records(path):
    data, output = path.read_bytes(), []
    while data:
        size = struct.unpack('!I', data[:4])[0]
        frame, data = data[4:4 + size], data[4 + size:]
        output.append((frame[0], frame[1], frame[2:]))
    return output


def test_canonical_target_wins_without_rewriting_original_bytes_and_databases(tmp_path):
    old = blob('Mixed.cache.test')
    target = blob('mixed.cache.test', b'\xc0\x00\x02\x02', lifetime=0)
    other_db = blob('mixed.cache.test', b'\xc0\x00\x02\x03')
    client = CopiedRedis({0: {key(old): old, key(target): target}, 1: {key(other_db): other_db}})
    path = tmp_path / 'catalog.bin'
    counts = replay['catalog'](client, {0: b'synthetic-seed', 1: b'synthetic-seed'}, path, lambda _: None)
    assert records(path) == [(0, 1, target), (1, 1, other_db)]
    assert counts['catalog_records'] == 2 and counts['duplicate_canonical_lookup'] == 1
    assert parse_value(records(path)[0][2])['expiry'] == 1000


def test_missing_target_markers_and_unverifiable_keys_are_reported(tmp_path):
    mixed = blob('Upper.cache.test')
    excluded = blob('unknown.cache.test')
    client = CopiedRedis({0: {key(mixed): mixed, b'A' * 64: b'\0' * 4, b'B' * 64: excluded}, 1: {}})
    path = tmp_path / 'catalog.bin'
    counts = replay['catalog'](client, {0: b'synthetic-seed', 1: b'synthetic-seed'}, path, lambda _: None)
    assert records(path) == [(0, 2, mixed)]
    assert counts['missing_canonical_target'] == counts['removal_markers'] == counts['unverifiable_key_preserved'] == 1
    assert counts['keys'] == 3


@pytest.mark.parametrize('change', ['non_dns', 'different_question'])
def test_unclassified_input_or_target_question_conflict_cannot_pass(tmp_path, change):
    mixed = blob('Upper.cache.test')
    data = ({b'not-a-cachedb-key': b'unclassified'} if change == 'non_dns' else
            {key(mixed): mixed, key(mixed, lower=True): blob('another.cache.test')})
    with pytest.raises(ValueError):
        replay['catalog'](CopiedRedis({0: data}), {0: b'synthetic-seed'}, tmp_path / 'catalog.bin', lambda _: None)
