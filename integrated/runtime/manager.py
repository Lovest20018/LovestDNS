"""Single mutation worker behind a local, permission-restricted JSON protocol."""
import json
import os
from pathlib import Path
import socketserver
import threading

from runtime.locking import maintenance_lock
from runtime.maintenance import ActionFailed, CoreActions
from runtime.tasks import TaskStore, validate_submission


class Manager:
    def __init__(self, actions, store):
        self.actions = actions
        self.store = store
        self.wakeup = threading.Event()

    def dispatch(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
            raise ValueError("Invalid manager request")
        operation = request["operation"]
        if operation == "submit" and set(request) == {"operation", "task"}:
            validate_submission(request["task"])
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
            return {"schema_version": 1, "apply_custom_mod": env.get("CNAUTO") != "no", "save_redis_snapshot": True}
        if operation == "plan" and set(request) == {"operation"}:
            with maintenance_lock(self.actions.state):
                return self.actions.plan()
        raise ValueError("Unsupported manager operation")

    def run(self):
        while True:
            task = self.store.claim()
            if task is None:
                self.wakeup.wait(1)
                self.wakeup.clear()
                continue
            try:
                result = self.actions.execute(task)
                self.store.finish(task["id"], "succeeded", result)
            except ActionFailed as exc:
                self.store.finish(task["id"], "failed", {"error": str(exc), **exc.details})
            except ValueError as exc:
                self.store.finish(task["id"], "failed", {"error": str(exc)[:500]})
            except Exception:
                # Never persist raw commands, environments or exception output.
                self.store.finish(task["id"], "failed", {"error": "维护任务失败，请检查管理服务和核心日志"})


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
                response = {"ok": False, "error": str(exc)[:500]}
            except Exception:
                response = {"ok": False, "error": "管理服务暂不可用"}
            try:
                self.wfile.write(json.dumps(response, allow_nan=False, ensure_ascii=False).encode() + b"\n")
            except (OSError, TimeoutError):
                pass

    path = run / "manager.sock"
    path.unlink(missing_ok=True)
    server = socketserver.UnixStreamServer(str(path), Handler)
    os.chmod(path, 0o600)
    threading.Thread(target=manager.run, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
