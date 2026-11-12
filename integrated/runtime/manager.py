"""Single mutation worker behind a local, permission-restricted JSON protocol."""
import json
import logging
import os
from pathlib import Path
import socketserver
import threading

from runtime.locking import MaintenanceBusy, maintenance_lock
from runtime.maintenance import ActionFailed, CoreActions
from runtime.tasks import TaskStore, validate_submission

LOGGER = logging.getLogger('paopaodns.manager')


class Manager:
    def __init__(self, actions, store):
        self.actions = actions
        self.store = store
        self.wakeup = threading.Event()
        self.worker = None
        self.phase = "starting"
        self.pending_result = None
        self.degraded = False
        self.claim_uncertain = False
        self.inspection = threading.BoundedSemaphore(1)

    def healthy(self):
        return bool(self.worker and self.worker.is_alive() and not self.degraded)

    def start(self):
        self.worker = threading.Thread(target=self.run, name="maintenance-worker", daemon=True)
        self.worker.start()

    def dispatch(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
            raise ValueError("Invalid manager request")
        operation = request["operation"]
        if operation == "submit" and set(request) == {"operation", "task"}:
            validate_submission(request["task"])
            if request["task"]["action"] == "restart_dns_core" and os.environ.get("LOVESTDNS_SYSTEMD") != "1":
                raise ValueError("网页重启仅适用于原生服务")
            previous = self.store.previous_submission(request["task"])
            if previous:
                return {"task": previous}
            if not self.healthy():
                raise RuntimeError("维护执行器暂不可用；请核对任务状态后重试原请求编号")
            self.actions.environment()
            task = self.store.submit(request["task"])
            self.wakeup.set()
            return {"task": task}
        if operation == "tasks" and set(request) == {"operation"}:
            return {"tasks": self.store.list()}
        if operation == "task" and set(request) == {"operation", "task"}:
            return {"task": self.store.get(request["task"])}
        if operation == "capabilities" and set(request) == {"operation"}:
            env = self.actions.environment()
            healthy = self.healthy()
            return {"schema_version": 1, "apply_custom_mod": healthy and env.get("CNAUTO") != "no", "save_redis_snapshot": healthy,
                    "restart_dns_core": healthy and os.environ.get("LOVESTDNS_SYSTEMD") == "1",
                    "worker": {"available": healthy, "phase": self.phase}}
        if operation in {"config_state", "plan"} and set(request) == {"operation"}:
            # No waiting preview queue: one active inspection, zero queued jobs.
            # Leave the other IPC threads available to tasks/capabilities reads.
            if not self.inspection.acquire(blocking=False):
                raise RuntimeError('配置检查正在进行，请稍后重试')
            try:
                with maintenance_lock(self.actions.state, timeout=0.05):
                    if operation == 'plan':
                        return self.actions.plan()
                    from runtime.config_state import status
                    return status(self.actions)
            finally:
                self.inspection.release()
        raise ValueError("Unsupported manager operation")

    def run(self):
        while True:
            self.step()
            self.wakeup.wait(1)
            self.wakeup.clear()

    def step(self):
        # Result persistence is a separate phase. Retry it without executing the
        # action again, even if SQLite is temporarily unavailable after success.
        if self.pending_result is None:
            try:
                self.phase = "claiming"
                if self.claim_uncertain:
                    self.store.interrupt_unconfirmed_claim()
                    self.claim_uncertain = False
                task = self.store.claim()
                self.degraded = False
            except Exception:
                if not self.degraded:
                    LOGGER.error('maintenance phase=journal_unavailable')
                self.degraded = True
                self.claim_uncertain = True
                self.phase = "journal_unavailable"
                return
            if task is None:
                self.phase = "idle"
                return
            self.phase = "executing"
            try:
                state, result = "succeeded", self.actions.execute(task)
            except ActionFailed as exc:
                state, result = "failed", {"error": str(exc), **exc.details}
            except MaintenanceBusy as exc:
                state, result = "failed", {"error": str(exc)}
            except ValueError as exc:
                state, result = "failed", {"error": str(exc)[:500]}
            except Exception:
                LOGGER.exception('maintenance phase=action_failed task_id=%s action=%s', task['id'], task['action'])
                state, result = "failed", {"error": "维护任务失败，请检查管理服务和核心日志"}
            self.pending_result = task["id"], state, result
        try:
            self.phase = "persisting_result"
            self.store.finish(*self.pending_result)
        except Exception:
            if self.phase != 'result_unconfirmed' and not self.degraded:
                LOGGER.error('maintenance phase=result_unconfirmed task_id=%s', self.pending_result[0])
            self.degraded = True
            self.phase = "result_unconfirmed"
            return
        self.pending_result = None
        self.degraded = False
        self.phase = "idle"


def main():
    os.umask(0o077)
    run = Path(os.environ.get("SUITE_RUN_DIR", "/run/paopaodns-suite"))
    state = Path(os.environ.get("SUITE_STATE_DIR", "/var/lib/paopaodns-suite"))
    manager = Manager(CoreActions(state, run), TaskStore(state))
    # Recovery is completed before the socket accepts new submissions.
    manager.store.recover()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(4)
            raw = self.rfile.readline(4097)
            try:
                if len(raw) > 4096 or not raw.endswith(b"\n"):
                    raise ValueError("Manager request too large")
                result = manager.dispatch(json.loads(raw))
                response = {"ok": True, "result": result}
            except (ValueError, RuntimeError) as exc:
                response = {"ok": False, "error": str(exc)[:500], "code": "UNAVAILABLE" if isinstance(exc, RuntimeError) else "CONFLICT"}
            except Exception:
                response = {"ok": False, "error": "管理服务暂不可用"}
            try:
                self.wfile.write(json.dumps(response, allow_nan=False, ensure_ascii=False).encode() + b"\n")
            except (OSError, TimeoutError):
                pass

    path = run / "manager.sock"
    path.unlink(missing_ok=True)
    # Bound admission before spawning threads. Slow previews cannot monopolize
    # tasks/capabilities reads, and mutations remain protected by the core lock.
    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(8)
            super().__init__(*args)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                try:
                    request.sendall(b'{"ok":false,"code":"UNAVAILABLE","error":"Manager busy"}\n')
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.slots.release()

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    manager.start()
    server.serve_forever()


if __name__ == "__main__":
    main()
