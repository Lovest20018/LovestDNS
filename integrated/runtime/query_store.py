"""Private, bounded SQLite event storage, separate from configuration/history."""
from collections import Counter
from contextlib import closing
import json
import sqlite3
import threading
import time

MAX_ROWS = 100000
MAX_AGE = 24 * 3600
DATABASE_BYTES = 31 * 1024 * 1024
STORAGE_BYTES = 64 * 1024 * 1024  # Reserve room for rollback page/header overhead too.
MAX_COVERAGE_INTERVALS = 2048
BLOCK_PREDICATE = "json_extract(payload,'$.filter.action') = 'block'"


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
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA page_size=4096')
            db.execute('PRAGMA journal_mode=TRUNCATE')
            db.execute(f'PRAGMA max_page_count={max_bytes // 4096}')
            db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL, source TEXT NOT NULL, domain TEXT NOT NULL, qtype INTEGER NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS by_time ON events(observed)')
            db.execute('CREATE TABLE IF NOT EXISTS coverage (start INTEGER PRIMARY KEY, end INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS retention (singleton INTEGER PRIMARY KEY CHECK(singleton=1), lost_before REAL NOT NULL)')
            self.row_count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        self.path.chmod(0o600)
        self.prune()
        # An existing dense database may already use every page. Keep its
        # history readable even when SQLite cannot allocate the optional index.
        with closing(self.connect()) as db, db:
            self.has_block_index = False
            try:
                db.execute(f'CREATE INDEX IF NOT EXISTS blocked_seq ON events(seq) WHERE {BLOCK_PREDICATE}')
                self.has_block_index = True
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA journal_mode=TRUNCATE')
        db.execute(f'PRAGMA max_page_count={self.max_bytes // 4096}')
        db.execute('PRAGMA journal_size_limit=0')
        return db

    def prune(self, now=None):
        with self.lock, closing(self.connect()) as db:
            with db:
                cutoff = (time.time() if now is None else now) - self.max_age
                db.execute('DELETE FROM coverage WHERE end <= ?', (cutoff,))
                removed = db.execute('DELETE FROM events WHERE observed < ?', (cutoff,)).rowcount
                count = self.row_count - removed
                if count > self.max_rows:
                    removed += self.evict(db, count - self.max_rows)
            self.row_count -= removed

    @staticmethod
    def mark_loss(db, timestamp):
        db.execute('INSERT INTO retention VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET lost_before=max(lost_before, excluded.lost_before)', (timestamp,))

    def evict(self, db, count):
        removed = db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?) RETURNING observed', (count,)).fetchall()
        if removed:
            self.mark_loss(db, max(row[0] for row in removed) + .001)
        return len(removed)

    def record_coverage(self, start, end):
        if start >= end:
            return
        with self.lock, closing(self.connect()) as db, db:
            previous = db.execute('SELECT start,end FROM coverage ORDER BY start DESC LIMIT 1').fetchone()
            if previous and previous[0] <= start <= previous[1]:
                db.execute('UPDATE coverage SET end=max(end,?) WHERE start=?', (end, previous[0]))
            else:
                db.execute('INSERT OR IGNORE INTO coverage VALUES(?,?)', (start, end))
                db.execute('DELETE FROM coverage WHERE start < (SELECT start FROM coverage ORDER BY start DESC LIMIT 1 OFFSET ?)',
                           (MAX_COVERAGE_INTERVALS - 1,))

    def invalidate_coverage_after(self, cutoff):
        with self.lock, closing(self.connect()) as db, db:
            db.execute('DELETE FROM coverage WHERE start >= ?', (cutoff,))
            db.execute('UPDATE coverage SET end=? WHERE end>?', (cutoff, cutoff))

    def append(self, events):
        if len(events) > 128:
            raise ValueError('event_batch_limit')
        with self.lock, closing(self.connect()) as db:
            values = [(e['observed_at'], e['source_ip'], e['domain'], e['qtype'], json.dumps(e, separators=(',', ':'), allow_nan=False)) for e in events[-self.max_rows:]]
            def insert():
                with db:
                    excess = max(0, self.row_count + len(values) - self.max_rows)
                    removed = self.evict(db, excess) if excess else 0
                    db.executemany('INSERT INTO events(observed,source,domain,qtype,payload) VALUES(?,?,?,?,?)', values)
                self.row_count += len(values) - removed
            try:
                insert()
            except sqlite3.OperationalError as exc:
                if 'full' not in str(exc).lower():
                    raise
                # FULL rolls back the batch. Bounded eviction+one retry avoids
                # growth, a retry loop, and any dependency on the DNS process.
                with db:
                    removed = self.evict(db, 1024)
                self.row_count -= removed
                insert()

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
        if response == 'no_response_observed':
            clauses.append("json_extract(payload,'$.outcome') = 'no_response_observed'")
        elif response:
            clauses.append("json_extract(payload,'$.rcode') = ?")
            args.append(response)
        if decision == 'block':
            clauses.append(BLOCK_PREDICATE)
        elif decision:
            clauses.append("json_extract(payload,'$.filter.action') = ?")
            args.append(decision)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            lost_before = self.lost_before(db, cutoff)
            latest = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            table = 'events INDEXED BY blocked_seq' if decision == 'block' and self.has_block_index else 'events'
            rows = db.execute(f'SELECT seq,payload FROM {table} WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq DESC LIMIT ?', (*args, limit + 1)).fetchall()
        return {'events': [dict(json.loads(payload), sequence=seq) for seq, payload in rows[:limit]],
                'snapshot': snapshot if snapshot is not None else (latest[0] if latest else 0),
                'next_before': rows[limit - 1][0] if len(rows) > limit else None,
                'history_changed': snapshot is not None and start is not None and start < lost_before}

    @staticmethod
    def lost_before(db, cutoff):
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
            db.execute('BEGIN')
            snapshot = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            lost_before = self.lost_before(db, cutoff)
            coverage = db.execute('SELECT start,end FROM coverage WHERE end>? AND start<? ORDER BY start',
                                  (max(start, lost_before), end)).fetchall()
            rows = db.execute('''SELECT observed,source,domain,
                CASE WHEN json_extract(payload,'$.outcome')='response_observed'
                     THEN json_extract(payload,'$.elapsed_ms') END,
                json_extract(payload,'$.filter.action'), json_extract(payload,'$.timing_scope')
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
        with self.lock, closing(self.connect()) as db:
            with db:
                db.execute('DELETE FROM events')
                db.execute('DELETE FROM coverage')
                self.mark_loss(db, time.time())
            self.row_count = 0

    def summary(self):
        # One daemon owns writes; maintain this count under the same lock rather
        # than scanning table pages after every small batch.
        with self.lock:
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
