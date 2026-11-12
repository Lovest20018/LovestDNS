"""Versioned evidence for unique cachedb seeding, including repeated attempts."""
import hashlib
import json

PROTOCOL = 'unique-question-coverage-v2'
METHOD = 'fixture-question-ledger-v1'
MAX_EXTRA_RATIO = .01


def coverage(expected, before, after, input_sha256):
    delta = after - before
    # Transport changes are retained in the ledger, but DNS question identity
    # consists of case-insensitive name, type and class.
    actual = {key[:3] for key, count in delta.items() if count > 0}
    attempts = sum(delta.values())
    ledger = sorted([*key, count] for key, count in delta.items())
    missing, unexpected = expected - actual, actual - expected
    extra = attempts - len(actual)
    return {'method': METHOD, 'input_sha256': input_sha256,
            'expected_unique': len(expected), 'observed_unique': len(actual),
            'missing_unique': len(missing), 'unexpected_unique': len(unexpected),
            'attempts': attempts, 'additional_attempts': extra,
            'transport_attempts': {kind: sum(v for k, v in delta.items() if k[3] == kind)
                                   for kind in ('udp', 'tcp')},
            'ledger_sha256': hashlib.sha256(json.dumps(ledger, separators=(',', ':')).encode()).hexdigest(),
            'complete': not missing and not unexpected and len(actual) == len(expected)}


def valid_seed_path(seed, entries, protocol=None):
    if protocol is None:
        # Keep historical reports under their original, stricter protocol.
        return seed.get('upstream_requests') == entries
    if protocol != PROTOCOL:
        return False
    row = seed.get('question_coverage') or {}
    counts = ('expected_unique', 'observed_unique', 'missing_unique', 'unexpected_unique',
              'attempts', 'additional_attempts')
    if not all(type(row.get(k)) is int and row[k] >= 0 for k in counts):
        return False
    transports = row.get('transport_attempts') or {}
    return (row.get('method') == METHOD and row.get('complete') is True
            and row.get('input_sha256') == seed.get('input_sha256')
            and row['expected_unique'] == row['observed_unique'] == entries
            and row['missing_unique'] == row['unexpected_unique'] == 0
            and row['attempts'] == seed.get('upstream_requests')
            and row['additional_attempts'] == row['attempts'] - entries
            and row['additional_attempts'] <= entries * MAX_EXTRA_RATIO
            and set(transports) == {'udp', 'tcp'}
            and all(type(v) is int and v >= 0 for v in transports.values())
            and sum(transports.values()) == row['attempts']
            and isinstance(row.get('ledger_sha256'), str) and len(row['ledger_sha256']) == 64)
