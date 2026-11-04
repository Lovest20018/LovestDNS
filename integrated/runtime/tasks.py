"""Persistent, bounded task journal; interrupted tasks are never replayed."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

ACTIONS = {"apply_custom_mod", "save_redis_snapshot", "restart_dns_core"}
ID = re.compile(r"^[a-f0-9]{32}$")
TERMINAL = {"succeeded", "failed", "interrupted"}


def validate_submission(payload):
    if not isinstance(payload, dict) or set(payload) != {"action", "request_id", "expected_sha256"}:
        raise ValueError("任务参数无效")
    if not isinstance(payload["action"], str) or payload["action"] not in ACTIONS:
        raise ValueError("不支持的维护动作")
    if not isinstance(payload["request_id"], str) or not ID.fullmatch(payload["request_id"]):
        raise ValueError("request_id 无效")
    checksum = payload["expected_sha256"]
    if payload["action"] == "apply_custom_mod":
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("请先预览当前配置")
    elif checksum is not None:
        raise ValueError("此任务不接受配置参数")


class TaskStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "maintenance-tasks.sqlite"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise ValueError("不支持的任务记录版本")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, action TEXT NOT NULL,
                expected_sha256 TEXT, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, result TEXT)""")
            db.execute("PRAGMA user_version=1")
            db.commit()
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def value(row):
        if row is None:
            return None
        item = dict(row)
        item.pop("request_id", None)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def submit(self, payload):
        validate_submission(payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM tasks WHERE request_id=?", (payload["request_id"],)).fetchone()
            if existing:
                if existing["action"] != payload["action"] or existing["expected_sha256"] != payload["expected_sha256"]:
                    raise ValueError("同一 request_id 不能用于不同任务")
                return self.value(existing)
            active = db.execute("SELECT * FROM tasks WHERE state IN ('queued','running')").fetchone()
            if active:
                if active["action"] == payload["action"] and active["expected_sha256"] == payload["expected_sha256"]:
                    return self.value(active)
                raise ValueError("已有维护任务进行中，请等待完成")
            task = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,NULL)",
                       (task, payload["request_id"], payload["action"], payload["expected_sha256"], "queued", now, now))
            db.execute("DELETE FROM tasks WHERE id IN (SELECT id FROM tasks WHERE state NOT IN ('queued','running') ORDER BY created DESC LIMIT -1 OFFSET 99)")
            return self.value(db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone())

    def previous_submission(self, payload):
        validate_submission(payload)
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE request_id=?", (payload["request_id"],)).fetchone()
            if row and (row["action"] != payload["action"] or row["expected_sha256"] != payload["expected_sha256"]):
                raise ValueError("同一 request_id 不能用于不同任务")
            return self.value(row)

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE tasks SET state='interrupted', updated=?, result=? WHERE state IN ('running','queued')",
                       (time.time(), json.dumps({"error": "管理进程重启，任务未自动重试；请核对组件和运行配置"}, ensure_ascii=False)))

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE tasks SET state='running', updated=? WHERE id=?", (time.time(), row["id"]))
                return self.value(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def finish(self, task, state, result):
        if state not in TERMINAL:
            raise ValueError("Invalid task completion state")
        with self.connect() as db:
            db.execute("UPDATE tasks SET state=?, updated=?, result=? WHERE id=? AND state='running'",
                       (state, time.time(), json.dumps(result, allow_nan=False, ensure_ascii=False), task))

    def list(self):
        with self.connect() as db:
            return [self.value(row) for row in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 20")]

    def get(self, task_id):
        if not isinstance(task_id, str) or not ID.fullmatch(task_id):
            raise ValueError("任务编号无效")
        with self.connect() as db:
            return self.value(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
