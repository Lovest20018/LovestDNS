"""Cross-process guard shared by configuration writes and core maintenance."""
from contextlib import contextmanager
import sqlite3
from pathlib import Path


class MaintenanceBusy(RuntimeError):
    pass


@contextmanager
def maintenance_lock(state_dir, timeout=2):
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "maintenance-lock.sqlite", timeout=timeout)
    try:
        try:
            db.execute("CREATE TABLE IF NOT EXISTS guard (id INTEGER PRIMARY KEY)")
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MaintenanceBusy("核心维护任务进行中，请稍后重试") from exc
        yield
        db.commit()
    finally:
        db.close()
