"""Private, bounded SQLite event storage, separate from configuration/history."""
from collections import Counter
from contextlib import closing, contextmanager
import json
import sqlite3
from sys import float_info
import threading
import time

MAX_ROWS = 100000
MAX_AGE = 24 * 3600
DATABASE_BYTES = 31 * 1024 * 1024
STORAGE_BYTES = 64 * 1024 * 1024  # Reserve room for rollback page/header overhead too.
MAX_COVERAGE_INTERVALS = 2048
BLOCK_PREDICATE = "json_extract(payload,'$.filter.action') = 'block'"
TYPED_COLUMNS = {
    'schema_ver': 'INTEGER NOT NULL DEFAULT 0',
    'outcome': 'TEXT',
    'rcode': 'TEXT',
    'elapsed_ms': 'REAL',
    'filter_action': 'TEXT',
    'timing_scope': 'TEXT',
}
RETENTION_SCHEMA = 'CREATE TABLE IF NOT EXISTS retention (singleton INTEGER PRIMARY KEY CHECK(singleton=1), lost_before REAL NOT NULL)'


class Store:
    def __init__(self, directory, max_rows=MAX_ROWS, max_age=MAX_AGE, max_bytes=DATABASE_BYTES):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError('query_directory_is_link')
        self.path = directory / 'events.sqlite'
        if self.path.is_symlink():
            raise ValueError('query_database_is_link')
        if self.path.exists() and self.path.stat().st_size > max_bytes:
            raise ValueError('query_database_exceeds_limit')
        self.lock = threading.RLock()
        self.max_rows, self.max_age, self.max_bytes = max_rows, max_age, max_bytes
        self._writer = None
        self.has_typed_columns = self.has_block_index = False
        self.has_coverage = self.has_retention = self.has_time_index = False
        columns = ', '.join(f'{name} {definition}' for name, definition in TYPED_COLUMNS.items())
        try:
            with self._write_connection() as db:
                db.execute(f'''CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL,
                    source TEXT NOT NULL, domain TEXT NOT NULL, qtype INTEGER NOT NULL,
                    payload TEXT NOT NULL, {columns})''')
                self.row_count = db.execute('SELECT count(*) FROM events').fetchone()[0]
                self._ensure_optional_schema(db)
            self.path.chmod(0o600)
            self.prune()
        except BaseException:
            self.close()
            raise

    def connect(self, *, writer=False):
        db = sqlite3.connect(self.path, timeout=2, check_same_thread=not writer)
        try:
            if writer:
                db.execute('PRAGMA page_size=4096')
                if db.execute('PRAGMA page_size').fetchone()[0] != 4096:
                    raise ValueError('query_database_page_size')
                # Rollback journal mode is connection-local. Readers leave it alone.
                if db.execute('PRAGMA journal_mode=TRUNCATE').fetchone()[0] != 'truncate':
                    raise sqlite3.OperationalError('query_database_journal_mode')
            page_limit = self.max_bytes // 4096
            if db.execute(f'PRAGMA max_page_count={page_limit}').fetchone()[0] != page_limit:
                raise ValueError('query_database_exceeds_limit')
            db.execute('PRAGMA journal_size_limit=0')
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _write_connection(self):
        with self.lock:
            if self._writer is not None:
                try:
                    # Check whether the connection is open without executing SQL.
                    self._writer.in_transaction
                except sqlite3.ProgrammingError:
                    self._writer = None
            if self._writer is None:
                self._writer = self.connect(writer=True)
            try:
                yield self._writer
            except sqlite3.Error:
                self.close()
                raise

    def close(self):
        with self.lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    @staticmethod
    def _create_if_space(db, statement):
        try:
            db.execute(statement)
        except sqlite3.Error as exc:
            if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                raise
            return False
        return True

    def _ensure_optional_schema(self, db):
        # Older databases can lack the coverage tables and have no free page.
        if not self.has_retention:
            self.has_retention = self._create_if_space(db, RETENTION_SCHEMA)
        if not self.has_coverage:
            self.has_coverage = self._create_if_space(db, 'CREATE TABLE IF NOT EXISTS coverage (start INTEGER PRIMARY KEY, end INTEGER NOT NULL)')
        if not self.has_time_index:
            self.has_time_index = self._create_if_space(db, 'CREATE INDEX IF NOT EXISTS by_time ON events(observed)')
        if not self.has_typed_columns:
            columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
            try:
                with db:
                    db.execute('BEGIN')
                    for name, definition in TYPED_COLUMNS.items():
                        if name not in columns:
                            db.execute(f'ALTER TABLE events ADD COLUMN {name} {definition}')
            except sqlite3.Error as exc:
                if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                    raise
            else:
                self.has_typed_columns = True
        if not self.has_block_index:
            self.has_block_index = self._create_if_space(
                db, f'CREATE INDEX IF NOT EXISTS blocked_seq ON events(seq) WHERE {BLOCK_PREDICATE}')

    def prune(self, now=None):
        with self._write_connection() as db:
            with db:
                cutoff = (time.time() if now is None else now) - self.max_age
                removed_coverage = (db.execute('DELETE FROM coverage WHERE end <= ?', (cutoff,)).rowcount
                                    if self.has_coverage else 0)
                removed = db.execute('DELETE FROM events WHERE observed < ?', (cutoff,)).rowcount
                count = self.row_count - removed
                evicted = self.evict(db, count - self.max_rows) if count > self.max_rows else 0
            self.row_count -= removed + evicted
            if evicted:
                self.has_retention = True
            if removed or evicted or removed_coverage:
                self._ensure_optional_schema(db)

    def mark_loss(self, db, timestamp):
        if not self.has_retention:
            db.execute(RETENTION_SCHEMA)
        db.execute('INSERT INTO retention VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET lost_before=max(lost_before, excluded.lost_before)', (timestamp,))

    def evict(self, db, count):
        removed = db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?) RETURNING observed', (count,)).fetchall()
        if removed:
            self.mark_loss(db, max(row[0] for row in removed) + .001)
        return len(removed)

    def record_coverage(self, start, end):
        if start >= end:
            return
        with self._write_connection() as db, db:
            if not self.has_coverage:
                return
            previous = db.execute('SELECT start,end FROM coverage ORDER BY start DESC LIMIT 1').fetchone()
            if previous and previous[0] <= start <= previous[1]:
                db.execute('UPDATE coverage SET end=max(end,?) WHERE start=?', (end, previous[0]))
            else:
                db.execute('INSERT OR IGNORE INTO coverage VALUES(?,?)', (start, end))
                db.execute('DELETE FROM coverage WHERE start < (SELECT start FROM coverage ORDER BY start DESC LIMIT 1 OFFSET ?)',
                           (MAX_COVERAGE_INTERVALS - 1,))

    def invalidate_coverage_after(self, cutoff):
        with self._write_connection() as db, db:
            if not self.has_coverage:
                return
            db.execute('DELETE FROM coverage WHERE start >= ?', (cutoff,))
            db.execute('UPDATE coverage SET end=? WHERE end>?', (cutoff, cutoff))

    def append(self, events):
        if len(events) > 128:
            raise ValueError('event_batch_limit')
        with self._write_connection() as db:
            columns, placeholders = 'observed,source,domain,qtype,payload', '?,?,?,?,?'
            if self.has_typed_columns:
                columns += ',schema_ver,outcome,rcode,elapsed_ms,filter_action,timing_scope'
                placeholders += ',1,?,?,?,?,?'
            values = []
            for event in events[-self.max_rows:]:
                row = (event['observed_at'], event['source_ip'], event['domain'], event['qtype'],
                       json.dumps(event, separators=(',', ':'), allow_nan=False))
                if self.has_typed_columns:
                    elapsed = event.get('elapsed_ms')
                    elapsed = (float(elapsed) if type(elapsed) in (int, float)
                               and -float_info.max <= elapsed <= float_info.max else None)
                    verdict = event.get('filter')
                    action = verdict.get('action') if isinstance(verdict, dict) else None
                    row += (event.get('outcome'), event.get('rcode'), elapsed, action, event.get('timing_scope'))
                values.append(row)

            def insert():
                with db:
                    excess = max(0, self.row_count + len(values) - self.max_rows)
                    removed = self.evict(db, excess) if excess else 0
                    db.executemany(f'INSERT INTO events({columns}) VALUES({placeholders})', values)
                self.row_count += len(values) - removed
                if removed:
                    self.has_retention = True

            try:
                insert()
            except sqlite3.Error as exc:
                if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                    raise
                # FULL rolls back the batch. Bounded eviction+one retry avoids
                # growth, a retry loop, and any dependency on the DNS process.
                with db:
                    removed = self.evict(db, 1024)
                self.row_count -= removed
                if removed:
                    self.has_retention = True
                insert()

    @staticmethod
    def _event_column(column, path, has_typed):
        legacy = f"json_extract(payload,'$.{path}')"
        return f'CASE WHEN schema_ver=1 THEN {column} ELSE {legacy} END' if has_typed else legacy

    def recent(self, limit=50, before=None, source=None, domain=None, qtype=None, *,
               exact=None, response=None, start=None, end=None, snapshot=None, decision=None):
        cutoff = time.time() - self.max_age
        clauses, args = ['observed >= ?'], [max(cutoff, start or 0)]
        if end is not None:
            clauses.append('observed < ?')
            args.append(end)
        if snapshot is not None:
            clauses.append('seq <= ?')
            args.append(snapshot)
        if before is not None:
            clauses.append('seq < ?')
            args.append(before)
        if source:
            clauses.append('source = ?')
            args.append(source)
        if domain:
            clauses.append("domain LIKE ? ESCAPE '\\'")
            args.append('%' + domain.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        if exact:
            clauses.append('domain IN (?,?)')
            args.extend((exact, exact if exact == '.' else exact + '.'))
        if qtype is not None:
            clauses.append('qtype = ?')
            args.append(qtype)
        with closing(self.connect()) as db:
            # A migration can publish new flags after SQLite fixes the read snapshot.
            has_typed = self.has_typed_columns
            has_block = self.has_block_index
            has_retention = self.has_retention
            db.execute('BEGIN')
            latest = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            lost_before = self.lost_before(db, cutoff, has_retention)
            if response:
                column = 'outcome' if response == 'no_response_observed' else 'rcode'
                clauses.append(f'{self._event_column(column, column, has_typed)} = ?')
                args.append(response)
            use_block_index = decision == 'block' and has_block
            if use_block_index:
                # The partial index already contains exactly the matching rows.
                clauses.append(BLOCK_PREDICATE)
            elif decision:
                clauses.append(f"{self._event_column('filter_action', 'filter.action', has_typed)} = ?")
                args.append(decision)
            table = 'events INDEXED BY blocked_seq' if use_block_index else 'events'
            rows = db.execute(f'SELECT seq,payload FROM {table} WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq DESC LIMIT ?', (*args, limit + 1)).fetchall()
        return {'events': [dict(json.loads(payload), sequence=seq) for seq, payload in rows[:limit]],
                'snapshot': snapshot if snapshot is not None else (latest[0] if latest else 0),
                'next_before': rows[limit - 1][0] if len(rows) > limit else None,
                'history_changed': snapshot is not None and start is not None and start < lost_before}

    @staticmethod
    def lost_before(db, cutoff, has_retention):
        if not has_retention:
            return cutoff
        row = db.execute('SELECT lost_before FROM retention WHERE singleton=1').fetchone()
        return max(cutoff, row[0]) if row else cutoff

    def analytics(self, hours, now=None):
        end = int(time.time() if now is None else now)
        start = end - hours * 3600
        width = 300 if hours == 1 else 3600
        cutoff = end - self.max_age
        # Read one SQLite snapshot without holding the producer's application lock.
        # Release it before counting, sorting or serializing.
        with closing(self.connect()) as db:
            has_typed = self.has_typed_columns
            has_retention = self.has_retention
            has_coverage = self.has_coverage
            db.execute('BEGIN')
            snapshot = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            lost_before = self.lost_before(db, cutoff, has_retention)
            coverage = []
            if has_coverage:
                coverage = db.execute('SELECT start,end FROM coverage WHERE end>? AND start<? ORDER BY start',
                                      (max(start, lost_before), end)).fetchall()
            rows = db.execute(f'''SELECT observed,source,domain,
                CASE WHEN {self._event_column('outcome', 'outcome', has_typed)}='response_observed'
                     THEN {self._event_column('elapsed_ms', 'elapsed_ms', has_typed)} END,
                {self._event_column('filter_action', 'filter.action', has_typed)},
                {self._event_column('timing_scope', 'timing_scope', has_typed)}
                FROM events WHERE observed>=? AND observed<?''', (max(cutoff, start), end)).fetchall()
        buckets = [{'start': left, 'end': left + width, 'count': 0,
                    'coverage': bucket_coverage(left, left + width, coverage, lost_before)}
                   for left in range(start, end, width)]
        sources, domains, blocked_sources, elapsed, scopes = Counter(), Counter(), Counter(), [], Counter()
        evaluated = 0
        for observed, source, domain, sample, action, scope in rows:
            buckets[int((observed - start) // width)]['count'] += 1
            sources[source] += 1
            domains[domain] += 1
            if type(sample) in (int, float) and 0 <= sample < float('inf'):
                elapsed.append(sample)
                scopes[scope or 'unknown'] += 1
            if action in ('block', 'allow', 'pass'):
                evaluated += 1
            if action == 'block':
                blocked_sources[source] += 1
        elapsed.sort()
        return {'start': start, 'end': end, 'snapshot': snapshot[0] if snapshot else 0,
                'total': len(rows), 'source_count': len(sources), 'latency_samples': len(elapsed), 'latency_scopes': dict(scopes),
                'p95_ms': elapsed[(95 * len(elapsed) + 99) // 100 - 1] if elapsed else None,
                'buckets': buckets, 'domains': rank(domains), 'sources': rank(sources),
                'evaluated': evaluated, 'blocked': sum(blocked_sources.values()), 'blocked_sources': rank(blocked_sources)}

    def clear(self):
        with self._write_connection() as db:
            with db:
                db.execute('DELETE FROM events')
                if self.has_coverage:
                    db.execute('DELETE FROM coverage')
                self.mark_loss(db, time.time())
            self.row_count = 0
            self.has_retention = True
            self._ensure_optional_schema(db)

    def summary(self):
        # The writer publishes the count after each commit. Sampling it here
        # lets status remain responsive while SQLite is writing the next batch.
        return {'retained_rows': self.row_count, 'database_bytes': self.path.stat().st_size,
                'limits': {'retention_seconds': self.max_age, 'rows': self.max_rows,
                           'storage_bytes': STORAGE_BYTES, 'database_bytes': DATABASE_BYTES}}


def rank(counts):
    return [{'value': value, 'count': count} for value, count in
            sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:10]]


def bucket_coverage(start, end, intervals, lost_before):
    for left, right in intervals:
        left = max(left, lost_before)
        if left <= start and end <= right:
            return 'complete'
        if max(left, start) < min(right, end):
            return 'partial'
    return 'unknown'
