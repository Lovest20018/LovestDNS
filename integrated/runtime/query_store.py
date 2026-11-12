"""Private, bounded SQLite event storage, separate from configuration/history."""
from contextlib import closing
import json
import sqlite3
import threading
import time

MAX_ROWS = 100000
MAX_AGE = 24 * 3600
DATABASE_BYTES = 31 * 1024 * 1024
STORAGE_BYTES = 64 * 1024 * 1024  # Reserve room for rollback page/header overhead too.


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
            self.row_count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        self.path.chmod(0o600)
        self.prune()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('PRAGMA journal_mode=TRUNCATE')
        db.execute(f'PRAGMA max_page_count={self.max_bytes // 4096}')
        db.execute('PRAGMA journal_size_limit=0')
        return db

    def prune(self, now=None):
        with self.lock, closing(self.connect()) as db:
            with db:
                removed = db.execute('DELETE FROM events WHERE observed < ?', ((time.time() if now is None else now) - self.max_age,)).rowcount
                count = self.row_count - removed
                if count > self.max_rows:
                    removed += db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?)', (count - self.max_rows,)).rowcount
            self.row_count -= removed

    def append(self, events):
        if len(events) > 128:
            raise ValueError('event_batch_limit')
        with self.lock, closing(self.connect()) as db:
            values = [(e['observed_at'], e['source_ip'], e['domain'], e['qtype'], json.dumps(e, separators=(',', ':'), allow_nan=False)) for e in events[-self.max_rows:]]
            def insert():
                with db:
                    excess = max(0, self.row_count + len(values) - self.max_rows)
                    removed = db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT ?)', (excess,)).rowcount if excess else 0
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
                    removed = db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY seq LIMIT 1024)').rowcount
                self.row_count -= removed
                insert()

    def recent(self, limit=50, before=None, source=None, domain=None, qtype=None):
        clauses, args = ['observed >= ?'], [time.time() - self.max_age]
        if before is not None:
            clauses.append('seq < ?')
            args.append(before)
        if source:
            clauses.append('source = ?')
            args.append(source)
        if domain:
            clauses.append("domain LIKE ? ESCAPE '\\'")
            args.append('%' + domain.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        if qtype is not None:
            clauses.append('qtype = ?')
            args.append(qtype)
        with self.lock, closing(self.connect()) as db:
            rows = db.execute('SELECT seq,payload FROM events WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq DESC LIMIT ?', (*args, limit + 1)).fetchall()
        return {'events': [dict(json.loads(payload), sequence=seq) for seq, payload in rows[:limit]],
                'next_before': rows[limit - 1][0] if len(rows) > limit else None}

    def clear(self):
        with self.lock, closing(self.connect()) as db:
            with db:
                db.execute('DELETE FROM events')
            self.row_count = 0

    def summary(self):
        # One daemon owns writes; maintain this count under the same lock rather
        # than scanning table pages after every small batch.
        with self.lock:
            return {'retained_rows': self.row_count, 'database_bytes': self.path.stat().st_size,
                    'limits': {'retention_seconds': self.max_age, 'rows': self.max_rows,
                               'storage_bytes': STORAGE_BYTES, 'database_bytes': DATABASE_BYTES}}
