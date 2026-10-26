"""Bounded SQLite configuration history outside the upstream data directory."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time


class History:
    def __init__(self, state_dir):
        self.root = Path(state_dir)

    @contextmanager
    def connection(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.root / "config-history.sqlite", timeout=10)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported history schema; restore a compatible state backup")
            db.execute("""CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY, filename TEXT NOT NULL, created REAL NOT NULL,
                sha256 TEXT NOT NULL, content BLOB NOT NULL, reason TEXT NOT NULL)""")
            db.execute("PRAGMA user_version=1")
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def write_lock(self):
        # SQLite serializes writers across Gunicorn processes. Keep the lock across
        # the file write too, so the revision and file ordering cannot diverge.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def record(db, filename, content, reason):
        checksum = hashlib.sha256(content).hexdigest()
        last = db.execute("SELECT sha256 FROM revisions WHERE filename=? ORDER BY id DESC LIMIT 1",
                          (filename,)).fetchone()
        if not last or last[0] != checksum:
            db.execute("INSERT INTO revisions(filename,created,sha256,content,reason) VALUES(?,?,?,?,?)",
                       (filename, time.time(), checksum, content, reason))

    @staticmethod
    def prune(db):
        db.execute("""DELETE FROM revisions WHERE id IN (
            SELECT id FROM (SELECT id, ROW_NUMBER() OVER (
                PARTITION BY filename ORDER BY id DESC) AS n FROM revisions) WHERE n>20)""")
        # Keep the newest revision per file even at the size limit.
        while db.execute("SELECT COALESCE(SUM(LENGTH(content)),0) FROM revisions").fetchone()[0] > 64 * 1024 * 1024:
            row = db.execute("""SELECT MIN(id) FROM revisions WHERE id NOT IN (
                SELECT MAX(id) FROM revisions GROUP BY filename)""").fetchone()
            if row[0] is None:
                break
            db.execute("DELETE FROM revisions WHERE id=?", row)

    def list(self, filename):
        with self.connection() as db:
            return [{"id": r[0], "created_at": r[1], "sha256": r[2], "bytes": r[3], "reason": r[4]}
                    for r in db.execute("""SELECT id,created,sha256,LENGTH(content),reason
                        FROM revisions WHERE filename=? ORDER BY id DESC LIMIT 20""", (filename,))]

    def read(self, filename, revision):
        with self.connection() as db:
            row = db.execute("SELECT content FROM revisions WHERE filename=? AND id=?",
                             (filename, revision)).fetchone()
        return bytes(row[0]).decode("utf-8").replace("\r\n", "\n") if row else None
