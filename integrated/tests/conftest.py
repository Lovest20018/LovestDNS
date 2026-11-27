from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "integrated/backend"), str(ROOT / "integrated"),
               str(ROOT / "web/backend"), str(ROOT / "packaging/integrated")]


@pytest.fixture
def sqlite_trace(monkeypatch):
    statements = []
    connect = sqlite3.connect

    class TracedConnection(sqlite3.Connection):
        def commit(self):
            statements.append("Connection.commit()")
            return super().commit()

    def traced(*args, **kwargs):
        db = connect(*args, factory=TracedConnection, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    return statements


@pytest.fixture
def file_symlink():
    def create(link, target):
        try:
            link.symlink_to(target)
        except OSError:
            if sys.platform == "win32":
                pytest.skip("file symlinks unavailable")
            raise

    return create
