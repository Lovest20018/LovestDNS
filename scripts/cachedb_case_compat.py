"""Conservative Unbound cachedb key aliases, restricted to disposable Redis copies.

The DNS message and its two big-endian timestamps are never rewritten.  A
lowercase lookup key may be added only when the old key can be independently
derived and all colliding old variants have identical bytes after normalizing
the question's ASCII case.  Conflicts remain available under their old keys.
"""
from collections import Counter, defaultdict
import contextlib
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time

FORMAT = 'unbound-cachedb-case-alias-v1'
MAX_VALUE = 65535 + 16
KEY = re.compile(rb'[0-9A-F]{64}\Z')


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def plan_hash(plan):
    return hashlib.sha256(canonical_json(plan)).hexdigest()


def wire_name(packet, offset, *, compressed):
    """Validate wire labels; never split a presentation-format DNS name."""
    labels, visited, size, end = [], set(), 1, None
    while True:
        if offset >= len(packet) or offset in visited:
            raise ValueError('Truncated or cyclic DNS name')
        visited.add(offset)
        length = packet[offset]
        if length & 0xc0 == 0xc0:
            if not compressed or offset + 1 >= len(packet):
                raise ValueError('Unsupported compressed question or truncated pointer')
            target = ((length & 0x3f) << 8) | packet[offset + 1]
            if target < 12 or target >= offset:
                raise ValueError('Invalid DNS compression pointer')
            if end is None:
                end = offset + 2
            offset = target
            continue
        if length & 0xc0 or length > 63:
            raise ValueError('Invalid DNS label')
        offset += 1
        if not length:
            return b''.join(bytes([len(label)]) + label for label in labels) + b'\0', end or offset
        if offset + length > len(packet):
            raise ValueError('Truncated DNS label')
        labels.append(packet[offset:offset + length])
        size += length + 1
        if size > 255:
            raise ValueError('DNS name exceeds the wire limit')
        offset += length


def parse_value(value):
    if not isinstance(value, bytes) or not 33 <= len(value) <= MAX_VALUE:
        raise ValueError('Unsupported cachedb value length')
    packet = value[:-16]
    _, flags, questions, answers, authority, additional = struct.unpack('!6H', packet[:12])
    if not flags & 0x8000 or flags & 0x7800 or flags & 0x0200 or questions != 1:
        raise ValueError('Not an ordinary complete DNS response with one question')
    if flags & 15 not in (0, 3, 6):
        raise ValueError('Unsupported cachedb response status')
    name, end = wire_name(packet, 12, compressed=False)
    if end + 4 > len(packet):
        raise ValueError('Truncated DNS question')
    kind, cls = struct.unpack('!HH', packet[end:end + 4])
    if not kind or not cls:
        raise ValueError('Invalid DNS question type or class')
    offset, rrtypes, answer_types = end + 4, [], []
    for index in range(answers + authority + additional):
        _, offset = wire_name(packet, offset, compressed=True)
        if offset + 10 > len(packet):
            raise ValueError('Truncated DNS record header')
        rrkind, _, _, length = struct.unpack('!HHIH', packet[offset:offset + 10])
        rrtypes.append(rrkind)
        if index < answers:
            answer_types.append(rrkind)
        offset += 10 + length
        if offset > len(packet):
            raise ValueError('Truncated DNS record data')
    if offset != len(packet):
        raise ValueError('Unparsed bytes after DNS records')
    timestamp, expiry = struct.unpack('!QQ', value[-16:])
    if not 0 < timestamp <= expiry < 2 ** 63 or expiry - timestamp > 2 ** 32 - 1:
        raise ValueError('Invalid cachedb timestamps')
    lower = name.lower()  # bytes.lower changes only ASCII A..Z, preserving label bytes.
    question = name + packet[end:end + 4]
    lowered = lower + packet[end:end + 4]
    normalized = value[:12] + lower + value[end:]
    return {'question': question, 'lower_question': lowered, 'type': kind, 'class': cls,
            'timestamp': timestamp, 'expiry': expiry, 'rcode': flags & 15, 'rrtypes': rrtypes,
            'partial_cname': 5 in answer_types and kind != 5 and kind not in answer_types,
            'normalized_sha256': hashlib.sha256(normalized).hexdigest()}


def lookup_key(question, seed):
    if not isinstance(seed, bytes) or b'\0' in seed or len(question) + len(seed) >= 1024:
        raise ValueError('Seed cannot be represented by the supported cachedb hash contract')
    return hashlib.sha256(question + seed).hexdigest().upper().encode()


class RedisCommandError(ValueError):
    def __init__(self, response):
        self.response = response
        super().__init__('Redis command failed: ' + response.decode('utf-8', 'replace'))


class Redis:
    """Small binary-safe RESP2 client; callers supply only self-created sockets."""
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(30)
        try:
            self.socket.connect(str(path))
            self.stream = self.socket.makefile('rb')
        except BaseException:
            self.socket.close()
            raise

    def close(self):
        self.stream.close()
        self.socket.close()

    def _read(self, depth=0):
        line = self.stream.readline(16385)
        if depth > 8 or len(line) > 16384 or not line.endswith(b'\r\n'):
            raise ValueError('Invalid Redis response framing')
        kind, content = line[:1], line[1:-2]
        if kind == b'+':
            return content
        if kind == b'-':
            raise RedisCommandError(content)
        if kind == b':':
            return int(content)
        if kind in (b'$', b'*'):
            length = int(content)
            if length == -1:
                return None
            if not 0 <= length <= 64 * 1024 * 1024:
                raise ValueError('Redis response exceeds the bounded client limit')
            if kind == b'*':
                if length > 10000:
                    raise ValueError('Redis response has too many elements')
                return [self._read(depth + 1) for _ in range(length)]
            data = self.stream.read(length)
            if len(data) != length or self.stream.read(2) != b'\r\n':
                raise ValueError('Truncated Redis bulk response')
            return data
        raise ValueError('Unsupported Redis response')

    def call(self, *args):
        encoded = [value if isinstance(value, bytes) else str(value).encode() for value in args]
        self.socket.sendall(b'*' + str(len(encoded)).encode() + b'\r\n' + b''.join(
            b'$' + str(len(value)).encode() + b'\r\n' + value + b'\r\n' for value in encoded))
        return self._read()


@contextlib.contextmanager
def disposable_redis(input_rdb, work, command, *, max_memory=2 * 1024 ** 3):
    """There is deliberately no CLI option accepting an existing Redis socket."""
    work = Path(work)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied = work / 'cache.rdb'
    original = file_hash(input_rdb)
    shutil.copyfile(input_rdb, copied)
    if file_hash(copied) != original:
        raise ValueError('The RDB changed while its isolated copy was being made')
    with tempfile.TemporaryDirectory(prefix='ldns-case-') as temp:
        path = Path(temp) / 'redis.sock'
        with (work / 'redis.log').open('xb') as log:
            process = subprocess.Popen([*map(str, command), '--port', '0', '--unixsocket', str(path),
                '--unixsocketperm', '700', '--dir', str(work.resolve()), '--dbfilename', 'cache.rdb',
                '--save', '', '--appendonly', 'no', '--maxmemory', str(max_memory),
                '--maxmemory-policy', 'noeviction'], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            client = None
            try:
                deadline = time.monotonic() + 120
                while client is None:
                    if process.poll() is not None:
                        raise RuntimeError('Disposable Redis exited; inspect its private log')
                    try:
                        client = Redis(path)
                        if client.call('PING') != b'PONG':
                            raise ValueError('Unexpected Redis readiness response')
                    except (OSError, RedisCommandError) as exc:
                        if isinstance(exc, RedisCommandError) and not exc.response.startswith(b'LOADING '):
                            raise
                        if client:
                            client.close()
                            client = None
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Disposable Redis readiness deadline')
                        time.sleep(.05)
                yield client, copied
            finally:
                if client:
                    client.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if file_hash(input_rdb) != original:
                    raise ValueError('Original input RDB changed during offline preparation')


def build_plan(client, seeds, *, max_keys=3000000, max_aliases=20000, max_alias_bytes=64 * 1024 ** 2):
    counts, categories, groups = Counter(), Counter(), defaultdict(dict)
    partial_targets = set()
    aliases_bytes = 0
    for db, seed in sorted(seeds.items()):
        client.call('SELECT', db)
        cursor, seen = b'0', set()
        while True:
            cursor, keys = client.call('SCAN', cursor, 'COUNT', 512)
            unique = []
            for key in keys:
                if key in seen:
                    counts['duplicate_scan_entries'] += 1
                    continue
                seen.add(key)
                counts['scanned_keys'] += 1
                if counts['scanned_keys'] > max_keys:
                    raise ValueError('Offline scan key budget exceeded')
                if KEY.fullmatch(key):
                    unique.append(key)
                else:
                    counts['non_cachedb_key'] += 1
            values = client.call('MGET', *unique) if unique else []
            for key, value in zip(unique, values):
                if value is None:
                    counts['non_string_or_expired'] += 1
                    continue
                try:
                    entry = parse_value(value)
                except ValueError:
                    counts['unsupported_value'] += 1
                    continue
                old, new = (lookup_key(entry[field], seed) for field in ('question', 'lower_question'))
                if key == new:
                    counts['already_lowercase_key'] += 1
                    continue
                if key != old:
                    counts['seed_or_key_mismatch'] += 1
                    continue
                counts['old_case_keys'] += 1
                aliases_bytes += len(value)
                if counts['old_case_keys'] > max_aliases or aliases_bytes > max_alias_bytes:
                    raise ValueError('Offline case alias budget exceeded')
                categories['type_' + str(entry['type'])] += 1
                categories['rcode_' + str(entry['rcode'])] += 1
                if 5 in entry['rrtypes']:
                    categories['contains_cname'] += 1
                if any(kind in entry['rrtypes'] for kind in (43, 46, 47, 48, 50, 51)):
                    categories['contains_dnssec_records'] += 1
                if entry['partial_cname']:
                    counts['partial_cname_preserved'] += 1
                    partial_targets.add((db, new))
                    continue
                expire_at = client.call('PEXPIRETIME', key)
                if expire_at == -2:
                    counts['expired_during_scan'] += 1
                    continue
                groups[(db, new)][key] = {'source_key': key.decode(), 'target_key': new.decode(),
                    'value_sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value),
                    'normalized_sha256': entry['normalized_sha256'], 'redis_expire_at_ms': expire_at,
                    'cachedb_timestamp': entry['timestamp'], 'cachedb_expiry': entry['expiry']}
            if cursor == b'0':
                break
    aliases, conflicts = [], []
    if partial_targets:
        counts['partial_cname_groups_preserved'] = len(partial_targets)
    for (db, target), members in sorted(groups.items()):
        if (db, target) in partial_targets:
            continue
        client.call('SELECT', db)
        if client.call('EXISTS', target):
            counts['existing_target_groups_preserved'] += 1
            continue
        rows = sorted(members.values(), key=lambda row: row['source_key'])
        equivalence = {(row['normalized_sha256'], row['redis_expire_at_ms']) for row in rows}
        if len(equivalence) != 1:
            counts['conflicting_groups_preserved'] += 1
            conflicts.append({'db': db, 'target_key': target.decode(), 'source_keys': [row['source_key'] for row in rows]})
            continue
        aliases.append({'db': db, **rows[0], 'equivalent_source_keys': [row['source_key'] for row in rows]})
    return {'format': FORMAT, 'counts': dict(counts), 'categories': dict(categories),
            'seed_sha256': {str(db): hashlib.sha256(seed).hexdigest() for db, seed in sorted(seeds.items())},
            'aliases': aliases, 'conflicts': conflicts,
            'limits': {'max_keys': max_keys, 'max_aliases': max_aliases, 'max_alias_bytes': max_alias_bytes},
            'semantics': 'Only missing lowercase keys are added; all original keys and opaque value bytes remain; timestamps and absolute Redis expiry are preserved.'}


ALIAS_SCRIPT = '''
local value = redis.call('GET', KEYS[1])
if not value then return 'source_expired' end
if value ~= ARGV[1] then return redis.error_reply('source changed') end
local expiry = redis.call('PEXPIRETIME', KEYS[1])
if tostring(expiry) ~= ARGV[2] then return redis.error_reply('source expiry changed') end
if redis.call('EXISTS', KEYS[2]) ~= 0 then return redis.error_reply('target appeared') end
if expiry >= 0 then
    local now = redis.call('TIME')
    if expiry <= now[1] * 1000 + math.floor(now[2] / 1000) then return 'source_expired' end
    return redis.call('SET', KEYS[2], value, 'NX', 'PXAT', expiry)
end
return redis.call('SET', KEYS[2], value, 'NX')
'''


def apply_plan(client, plan):
    if plan.get('format') != FORMAT:
        raise ValueError('Unsupported offline plan format')
    counts = Counter()
    # Preflight the complete plan before the first write.  Lua then repeats each
    # check atomically, so a changed source or appeared target cannot be copied.
    values = []
    for row in plan['aliases']:
        client.call('SELECT', row['db'])
        source, target = row['source_key'].encode(), row['target_key'].encode()
        if not KEY.fullmatch(source) or not KEY.fullmatch(target) or source == target:
            raise ValueError('Invalid alias key pair')
        value = client.call('GET', source)
        if value is None and row['redis_expire_at_ms'] >= 0 and row['redis_expire_at_ms'] <= time.time() * 1000:
            values.append(None)
            continue
        if value is None or hashlib.sha256(value).hexdigest() != row['value_sha256']:
            raise ValueError('Source value differs from the reviewed plan')
        if client.call('PEXPIRETIME', source) != row['redis_expire_at_ms'] or client.call('EXISTS', target):
            raise ValueError('Source expiry or target differs from the reviewed plan')
        values.append(value)
    for row, value in zip(plan['aliases'], values):
        if value is None:
            counts['source_expired'] += 1
            continue
        client.call('SELECT', row['db'])
        result = client.call('EVAL', ALIAS_SCRIPT, 2, row['source_key'], row['target_key'], value, row['redis_expire_at_ms'])
        if result == b'source_expired':
            counts['source_expired'] += 1
        elif result == b'OK':
            counts['aliases_added'] += 1
            actual = client.call('GET', row['target_key'])
            if actual is None and row['redis_expire_at_ms'] >= 0 and row['redis_expire_at_ms'] <= time.time() * 1000:
                counts['target_expired_after_copy'] += 1
            elif (actual != value or client.call('PEXPIRETIME', row['target_key']) != row['redis_expire_at_ms']):
                raise ValueError('New alias does not preserve its source bytes and expiry')
        else:
            raise ValueError('Alias was not created')
    return dict(counts)
