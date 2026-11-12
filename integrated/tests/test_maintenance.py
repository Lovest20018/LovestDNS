import json
import threading
import time
from pathlib import Path

import pytest

from runtime import maintenance
from runtime.maintenance import ActionFailed, CoreActions
from runtime.locking import MaintenanceBusy, maintenance_lock
from runtime.manager import Manager
from runtime.tasks import TaskStore
from test_configuration import template


class FakeCore(CoreActions):
    def __init__(self, root):
        super().__init__(root / "state", root / "run", root / "data", root / "tmp")
        for directory in (self.state, self.run, self.data, self.temporary):
            directory.mkdir(parents=True)
        (self.run / "ready").touch()
        (self.run / "core-env.json").write_text(json.dumps({"CNAUTO": "yes"}))
        (self.run / "mosdns-env.json").write_text('{"CNAUTO":"yes","LOCAL_IP":"192.0.2.5"}')
        (self.data / "custom_env.ini").write_text('LOCAL_IP="192.0.2.10"\n')
        (self.data / "custom_mod.yaml").write_text('Hosts:\n- env_key: LOCAL_IP\n  zone: nas.example\n')
        (self.temporary / "mosdns_base.yaml").write_text(template())
        (self.temporary / "mosdns.yaml").write_text("old running configuration")
        self.processes = {name: {"statename": "RUNNING", "pid": i + 1, "start": 1} for i, name in enumerate(("mosdns", "watcher", "redis"))}
        self.events = []
        self.fail_start = False

    def states(self):
        return self.processes

    def control(self, action, component):
        self.events.append((action, component))
        if action == "start" and component == "mosdns" and self.fail_start:
            self.fail_start = False
            raise ValueError("candidate core rejected")
        self.processes[component]["statename"] = "RUNNING" if action == "start" else "STOPPED"

    def wait_running(self, component, timeout=8):
        if self.processes[component]["statename"] != "RUNNING":
            raise ValueError("Not running")
        return self.processes[component]

    def probe(self):
        return {"responded": True, "rcode": 3}


def test_apply_uses_preview_and_records_unverified_scope(tmp_path):
    core = FakeCore(tmp_path)
    plan = core.plan()
    result = core.apply(plan["sha256"])
    assert result["verified"] is False
    assert result["state"] == "applied_unverified"
    assert "old running" not in (core.temporary / "mosdns.yaml").read_text()
    assert json.loads((core.run / "mosdns-env.json").read_text())["LOCAL_IP"] == "192.0.2.10"
    assert core.events == [("stop", "watcher"), ("stop", "mosdns"), ("start", "mosdns"), ("start", "watcher")]
    assert (core.state / "last-application.json").exists()


def test_accepted_apply_waits_for_a_background_reload_guard(tmp_path):
    core = FakeCore(tmp_path)
    expected = core.plan()['sha256']
    held,release = threading.Event(),threading.Event()
    def reload():
        with maintenance_lock(core.state):
            held.set()
            release.wait(10)
    worker = threading.Thread(target=reload)
    worker.start()
    assert held.wait(3)
    timer = threading.Timer(3,release.set)
    timer.start()
    try:
        result = core.apply(expected)
        assert result['state'] == 'applied_unverified'
    finally:
        release.set();timer.cancel();worker.join(5)


def test_exhausted_guard_wait_returns_the_explicit_busy_message(tmp_path,monkeypatch):
    core = FakeCore(tmp_path)
    store = TaskStore(core.state)
    task = store.submit({'action':'apply_custom_mod','request_id':'a'*32,'expected_sha256':'b'*64})
    def busy(_): raise MaintenanceBusy('核心维护任务进行中，请稍后重试')
    monkeypatch.setattr(core,'execute',busy)
    Manager(core,store).step()
    result = store.get(task['id'])
    assert result['state'] == 'failed' and result['result']['error'] == '核心维护任务进行中，请稍后重试'


def test_stale_preview_never_stops_core(tmp_path):
    core = FakeCore(tmp_path)
    checksum = core.plan()["sha256"]
    (core.data / "custom_env.ini").write_text('LOCAL_IP="192.0.2.11"\n')
    with pytest.raises(ValueError, match="已改变"):
        core.apply(checksum)
    assert core.events == []


def test_failed_start_restores_previous_runtime_and_environment(tmp_path):
    core = FakeCore(tmp_path)
    old_env = (core.run / "mosdns-env.json").read_bytes()
    core.fail_start = True
    with pytest.raises(ActionFailed) as failure:
        core.apply(core.plan()["sha256"])
    assert failure.value.details["rollback"]["runtime_restored"] is True
    assert failure.value.details["rollback"]["service_running"] is True
    assert (core.temporary / "mosdns.yaml").read_text() == "old running configuration"
    assert (core.run / "mosdns-env.json").read_bytes() == old_env
    assert "192.0.2.10" in (core.data / "custom_env.ini").read_text()
    assert core.processes["watcher"]["statename"] == "RUNNING"
    assert not (core.state / "last-application.json").exists()


def test_guard_blocks_concurrent_config_writer(tmp_path):
    with maintenance_lock(tmp_path):
        with pytest.raises(MaintenanceBusy):
            with maintenance_lock(tmp_path, timeout=0.01):
                pytest.fail("Concurrent writer entered")


def test_task_idempotency_serialization_and_crash_recovery(tmp_path):
    store = TaskStore(tmp_path)
    payload = {"action": "apply_custom_mod", "request_id": "a" * 32, "expected_sha256": "b" * 64}
    first = store.submit(payload)
    assert store.submit(payload)["id"] == first["id"]
    with pytest.raises(ValueError):
        store.submit({"action": "save_redis_snapshot", "request_id": "c" * 32, "expected_sha256": None})
    running = store.claim()
    assert running["state"] == "running"
    assert store.claim() is None
    store.recover()
    assert store.list()[0]["state"] == "interrupted"
    assert store.claim() is None
    assert store.submit(payload)["state"] == "interrupted"


def test_coalesced_request_alias_survives_completion_and_restart(tmp_path):
    store = TaskStore(tmp_path)
    first = {"action": "save_redis_snapshot", "request_id": "a" * 32, "expected_sha256": None}
    alias = {**first, "request_id": "b" * 32}
    task = store.submit(first)
    assert store.submit(alias)["id"] == task["id"]
    store.claim()
    store.finish(task["id"], "succeeded", {"state": "saved"})
    reopened = TaskStore(tmp_path)
    assert reopened.submit(alias)["id"] == task["id"]
    assert reopened.previous_submission(alias)["state"] == "succeeded"
    with pytest.raises(ValueError, match="不同任务"):
        reopened.submit({**alias, "action": "restart_dns_core"})
    assert reopened.claim() is None


def test_worker_retries_result_persistence_without_reexecuting(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.submit({"action": "save_redis_snapshot", "request_id": "a" * 32, "expected_sha256": None})
    manager = Manager(FakeCore(tmp_path), store)
    calls = []
    monkeypatch.setattr(manager.actions, "execute", lambda task: calls.append(task["id"]) or {"state": "saved"})
    finish = store.finish
    monkeypatch.setattr(store, "finish", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    manager.step()
    assert manager.phase == "result_unconfirmed" and manager.degraded
    manager.step()
    assert calls == [task["id"]]
    monkeypatch.setattr(store, "finish", finish)
    manager.step()
    assert not manager.degraded and store.get(task["id"])["state"] == "succeeded"
    assert calls == [task["id"]]


def test_unexpected_action_error_is_logged_while_api_keeps_generic_error(tmp_path, monkeypatch, caplog):
    core = FakeCore(tmp_path)
    store = TaskStore(core.state)
    task = store.submit({"action":"save_redis_snapshot","request_id":"a"*32,"expected_sha256":None})
    manager = Manager(core,store)
    def fail(_): raise OSError('internal diagnostic detail')
    monkeypatch.setattr(core,'execute',fail)
    manager.step()
    result = store.get(task['id'])
    assert result['state'] == 'failed' and 'internal diagnostic detail' not in result['result']['error']
    logged = next(r for r in caplog.records if 'phase=action_failed' in r.message)
    assert task['id'] in logged.message and logged.exc_info[0] is OSError


def test_worker_claim_failure_is_visible_and_recoverable(tmp_path, monkeypatch):
    core = FakeCore(tmp_path)
    manager = Manager(core, TaskStore(core.state))
    claim = manager.store.claim
    monkeypatch.setattr(manager.store, "claim", lambda: (_ for _ in ()).throw(OSError("busy")))
    manager.step()
    assert manager.phase == "journal_unavailable"
    assert manager.dispatch({"operation": "capabilities"})["save_redis_snapshot"] is False
    monkeypatch.setattr(manager.store, "claim", claim)
    manager.step()
    assert manager.phase == "idle" and not manager.degraded


def test_claim_commit_response_loss_never_replays_or_leaves_running_forever(tmp_path, monkeypatch):
    core = FakeCore(tmp_path)
    store = TaskStore(core.state)
    task = store.submit({"action": "save_redis_snapshot", "request_id": "c" * 32, "expected_sha256": None})
    manager = Manager(core, store)
    claim = store.claim
    def uncertain_claim():
        claim()
        raise OSError('Commit response unavailable')
    monkeypatch.setattr(store, 'claim', uncertain_claim)
    monkeypatch.setattr(core, 'execute', lambda task: pytest.fail('Unconfirmed task was replayed'))
    manager.step()
    assert store.get(task['id'])['state'] == 'running'
    monkeypatch.setattr(store, 'claim', claim)
    manager.step()
    assert store.get(task['id'])['state'] == 'interrupted'
    assert manager.phase == 'idle'


@pytest.mark.parametrize("payload", [
    {"action": "shell", "request_id": "a" * 32, "expected_sha256": None},
    {"action": "apply_custom_mod", "request_id": "a" * 32, "expected_sha256": None},
    {"action": "save_redis_snapshot", "request_id": "../../bad", "expected_sha256": None},
    {"action": "save_redis_snapshot", "request_id": "a" * 32, "expected_sha256": None, "command": "id"},
])
def test_task_arguments_reject_commands_and_paths(tmp_path, payload):
    with pytest.raises(ValueError):
        TaskStore(tmp_path).submit(payload)


def test_manager_dispatch_whitelists_operations(tmp_path):
    core = FakeCore(tmp_path)
    manager = Manager(core, TaskStore(core.state))
    assert manager.dispatch({"operation": "capabilities"})["worker"]["available"] is False
    with pytest.raises(ValueError):
        manager.dispatch({"operation": "plan", "path": "/etc/passwd"})
    with pytest.raises(ValueError):
        manager.dispatch({"operation": "shell"})


def test_snapshot_confirms_new_successful_rdb(tmp_path, monkeypatch):
    core = FakeCore(tmp_path)
    old = int(time.time()) - 10
    (core.data / "redis_dns_v2.rdb").write_bytes(b"REDIS0011payload")
    before = {"loading": 0, "rdb_bgsave_in_progress": 0, "rdb_last_save_time": old}
    after = {**before, "rdb_last_save_time": int(time.time()), "rdb_last_bgsave_status": "ok", "rdb_changes_since_last_save": 3}
    replies = iter((before, after))
    monkeypatch.setattr(maintenance, "redis_stats", lambda: next(replies))
    calls = []
    monkeypatch.setattr(core, "bgsave", lambda: calls.append("BGSAVE"))
    result = core.save_snapshot()
    assert calls == ["BGSAVE"]
    assert result["state"] == "saved"
    assert result["changes_after_save"] == 3


def test_snapshot_save_error_does_not_report_success(tmp_path, monkeypatch):
    core = FakeCore(tmp_path)
    old = int(time.time()) - 10
    replies = iter(({"loading": 0, "rdb_bgsave_in_progress": 0, "rdb_last_save_time": old},
                    {"rdb_bgsave_in_progress": 0, "rdb_last_bgsave_status": "err"}))
    monkeypatch.setattr(maintenance, "redis_stats", lambda: next(replies))
    monkeypatch.setattr(core, "bgsave", lambda: None)
    with pytest.raises(ValueError, match="保存失败"):
        core.save_snapshot()
