from contextlib import closing
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from history import History
from runtime.dns_filter import FilterManager
from runtime.manager import Manager
from runtime.tasks import TaskStore


def test_empty_task_claim_uses_only_reads_with_an_existing_writer(tmp_path, sqlite_trace):
    store = TaskStore(tmp_path)
    assert store.claim() is None  # Initialize the schema.
    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        sqlite_trace.clear()
        assert store.claim() is None
        assert sqlite_trace == ["PRAGMA user_version", "SELECT 1 FROM tasks WHERE state='queued' LIMIT 1"]


def test_v1_task_store_migrates_existing_submissions(tmp_path):
    store = TaskStore(tmp_path)
    payload = {"action": "save_redis_snapshot", "request_id": "1" * 32, "expected_sha256": None}
    task = store.submit(payload)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE submissions")
        db.execute("PRAGMA user_version=1")
    assert store.claim()["id"] == task["id"]
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("SELECT request_id, task_id FROM submissions").fetchall() == [(payload["request_id"], task["id"])]


def test_history_pending_check_uses_only_reads_with_an_existing_writer(tmp_path, sqlite_trace):
    history = History(tmp_path)
    assert not history.has_pending("custom_env.ini")
    with closing(sqlite3.connect(tmp_path / "config-history.sqlite")) as writer:
        writer.execute("BEGIN IMMEDIATE")
        sqlite_trace.clear()
        assert not history.has_pending("custom_env.ini")
        assert sqlite_trace == ["PRAGMA user_version", "SELECT 1 FROM write_intents WHERE filename='custom_env.ini'"]


class StopWorker(Exception):
    pass


@pytest.mark.parametrize('pending,degraded', [(('task', 'succeeded', {}), False), (None, True)])
def test_manager_retries_recovery_after_one_second(tmp_path, monkeypatch, pending, degraded):
    worker = Manager(None, TaskStore(tmp_path))
    worker.pending_result, worker.degraded = pending, degraded
    worker.idle_wait = 30
    monkeypatch.setattr(worker, 'step', lambda: None)

    def check_interval(timeout):
        assert timeout == 1
        raise StopWorker

    monkeypatch.setattr(worker.wakeup, 'wait', check_interval)
    with pytest.raises(StopWorker):
        worker.run()


@pytest.mark.parametrize("worker_kind", ["maintenance", "filter"])
@pytest.mark.parametrize("arrival", ["during_step", "during_wait"])
def test_submit_wakes_idle_worker(tmp_path, monkeypatch, worker_kind, arrival):
    entered_step, release_step, waiting, stop, finished = (threading.Event() for _ in range(5))
    executed = []

    class Wakeup(threading.Event):
        def wait(self, timeout=None):
            assert timeout == 30
            waiting.set()
            return super().wait(timeout)

    if worker_kind == "maintenance":
        store = TaskStore(tmp_path / "state")
        worker = Manager(SimpleNamespace(environment=dict, execute=lambda job: executed.append(job) or {}), store)
        worker.idle_wait = 30

        def submit():
            payload = {"action": "save_redis_snapshot", "request_id": "2" * 32, "expected_sha256": None}
            return worker.dispatch({"operation": "submit", "task": payload})["task"]["id"]

        def job_state(job_id):
            return store.get(job_id)["state"]
    else:
        worker = FilterManager(tmp_path / "state", tmp_path / "run")
        worker.poll_interval = 30
        monkeypatch.setattr(worker, "execute", lambda job: executed.append(job) or "applied")
        monkeypatch.setattr(worker, "core_status", lambda: ("unavailable", None))

        def submit():
            return worker.submit({"action": "configure", "revision": "", "enabled": True, "local_rules": ""})["id"]

        def job_state(job_id):
            return worker.jobs[job_id]["phase"]

    worker.wakeup = Wakeup()
    step = worker.step

    def observe_step():
        if stop.is_set():
            raise StopWorker
        step()
        if not entered_step.is_set():
            entered_step.set()
            if arrival == "during_step":
                assert release_step.wait(3)
        if executed:
            finished.set()

    def run():
        try:
            worker.run()
        except StopWorker:
            pass

    monkeypatch.setattr(worker, "step", observe_step)
    thread = threading.Thread(target=run, daemon=True)
    if worker_kind == "maintenance":
        worker.worker = thread
    thread.start()
    try:
        ready = entered_step if arrival == "during_step" else waiting
        assert ready.wait(3)
        job_id = submit()
        release_step.set()
        assert finished.wait(.5)
        assert job_state(job_id) == "succeeded"
        assert len(executed) == 1
    finally:
        stop.set()
        release_step.set()
        worker.wakeup.set()
        thread.join(3)
        assert not thread.is_alive()
