"""Fixed core actions. All mutations run in one worker under the shared guard."""
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time
import xmlrpc.client

from runtime.configuration import prepare, read_bounded
from runtime.locking import maintenance_lock
from runtime.observer import UnixTransport, process_status, redis_stats


class ActionFailed(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


def atomic_file(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".suite-new")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class CoreActions:
    def __init__(self, state_dir, run_dir, data_dir="/data", temporary_dir="/tmp"):
        self.state = Path(state_dir)
        self.run = Path(run_dir)
        self.data = Path(data_dir)
        self.temporary = Path(temporary_dir)

    def environment(self):
        if not (self.run / "ready").exists():
            raise ValueError("核心尚未完成初始化")
        env = json.loads(read_bounded(self.run / "core-env.json").decode())
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("核心启动环境无效")
        return env

    def plan(self):
        env = self.environment()
        if env.get("CNAUTO") == "no":
            raise ValueError("纯递归模式不支持 custom_mod 应用")
        values = prepare(self.data, self.temporary / "mosdns_base.yaml", env)
        return {"sha256": values["sha256"], "inputs": values["inputs"],
                "candidate_sha256": hashlib.sha256(values["candidate"].encode()).hexdigest(),
                "candidate": values["candidate"],
                "notice": "应用会重启 MosDNS，可能短暂影响解析；组件启动与本地探测不证明每条规则已命中。失败时尝试回退运行配置。"}

    def control(self, action, component):
        if action not in {"start", "stop"} or component not in {"mosdns", "watcher"}:
            raise ValueError("Unsupported core action")
        with xmlrpc.client.ServerProxy("http://localhost/RPC2", transport=UnixTransport()) as rpc:
            if action == "stop":
                rpc.supervisor.stopProcess(component, True)
            else:
                rpc.supervisor.startProcess(component, True)

    def states(self):
        return process_status()

    def wait_running(self, component, timeout=8):
        deadline = time.monotonic() + timeout
        original = None
        while time.monotonic() < deadline:
            current = self.states().get(component, {})
            if current.get("statename") == "RUNNING":
                identity = (current.get("pid"), current.get("start"))
                if identity == original:
                    return current
                original = identity
            else:
                original = None
            time.sleep(0.5)
        raise ValueError(f"{component} 未能稳定启动")

    def probe(self):
        # A private reserved name may return NXDOMAIN; this checks protocol
        # responsiveness only, not the newly configured rules or public network.
        query_id = int.from_bytes(os.urandom(2), "big")
        question = b"\x11suite-ready-check\x07invalid\x00" + struct.pack("!HH", 1, 1)
        query = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + question
        address = os.environ.get("DNS_TEST_SERVER", "127.0.0.1")
        with socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(4)
            sock.connect((address, int(os.environ.get("SUITE_DNS_PORT", "53"))))
            sock.send(query)
            response = sock.recv(65535)
        if len(response) < 12 or response[:2] != query[:2] or not response[2] & 0x80:
            raise ValueError("MosDNS 未返回有效的探测应答")
        return {"responded": True, "rcode": response[3] & 15,
                "scope": "仅验证服务响应，不验证规则命中或外部域名解析"}

    def apply(self, expected):
        with maintenance_lock(self.state):
            env = self.environment()
            if env.get("CNAUTO") == "no":
                raise ValueError("纯递归模式不支持 custom_mod 应用")
            planned = prepare(self.data, self.temporary / "mosdns_base.yaml", env)
            if planned["sha256"] != expected:
                raise ValueError("配置或引用文件已改变，请重新预览后再应用")
            from runtime.config_state import inputs, record
            recorded_inputs = inputs(self.data)
            states = self.states()
            if any(states.get(name, {}).get("statename") != "RUNNING" for name in ("mosdns", "watcher")):
                raise ValueError("MosDNS 或监听器未正常运行，请先检查核心状态")
            target = self.temporary / "mosdns.yaml"
            old_config = read_bounded(target)
            active_env = self.run / "mosdns-env.json"
            old_env = active_env.read_bytes() if active_env.exists() else None
            previous = {"configuration": old_config.hex(), "environment": old_env.hex() if old_env is not None else None}
            # Durable copy for operator recovery if this process is interrupted.
            atomic_file(self.state / "last-runtime-before-apply.json", json.dumps(previous).encode())
            watcher_stopped = False
            stop_requested = False
            changed = False
            try:
                self.control("stop", "watcher")
                watcher_stopped = True
                stop_requested = True
                self.control("stop", "mosdns")
                # Recheck after the watcher is stopped, before replacing runtime data.
                fresh = prepare(self.data, self.temporary / "mosdns_base.yaml", env)
                if fresh["sha256"] != expected:
                    raise ValueError("配置在应用期间被外部修改，请重新预览")
                changed = True
                atomic_file(target, fresh["candidate"].encode())
                atomic_file(active_env, json.dumps(fresh["environment"]).encode())
                self.control("start", "mosdns")
                self.wait_running("mosdns")
                probe = self.probe()
                post = prepare(self.data, self.temporary / "mosdns_base.yaml", env)
                if post["sha256"] != expected:
                    raise ValueError("应用后检测到外部配置变化")
                self.control("start", "watcher")
                watcher_stopped = False
                self.wait_running("watcher")
                record(self, "apply_custom_mod", recorded_inputs)
                result = {"input_sha256": expected, "runtime_sha256": hashlib.sha256(fresh["candidate"].encode()).hexdigest(),
                          "state": "applied_unverified", "verified": False, "probe": probe,
                          "message": "配置已加载，服务已响应；请查询目标域名验证各条规则"}
                atomic_file(self.state / "last-application.json", json.dumps({**result, "completed_at": time.time()}).encode())
                return result
            except Exception as exc:
                if not stop_requested:
                    raise ActionFailed("无法暂停配置监听，未更改运行配置") from exc
                rollback = {"attempted": True, "runtime_restored": False, "service_running": False}
                try:
                    current = self.states().get("mosdns", {}).get("statename")
                    if current in {"RUNNING", "STARTING", "BACKOFF"}:
                        self.control("stop", "mosdns")
                    if changed:
                        atomic_file(target, old_config)
                        if old_env is None:
                            active_env.unlink(missing_ok=True)
                        else:
                            atomic_file(active_env, old_env)
                    rollback["runtime_restored"] = True
                    self.control("start", "mosdns")
                    self.wait_running("mosdns")
                    rollback["service_running"] = True
                except Exception:
                    rollback["error"] = "运行配置回退或服务恢复失败，请检查核心日志"
                raise ActionFailed("配置应用失败；保存的目标配置仍保留，查看回退结果后处理", {"rollback": rollback}) from exc
            finally:
                if watcher_stopped:
                    try:
                        self.control("start", "watcher")
                    except Exception:
                        pass

    @staticmethod
    def bgsave():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect("/tmp/redis.sock")
            sock.sendall(b"*1\r\n$6\r\nBGSAVE\r\n")
            with sock.makefile("rb") as stream:
                reply = stream.readline(512)
        if not reply.startswith(b"+") or not reply.endswith(b"\r\n"):
            raise ValueError("Redis 未接受后台保存请求，可能已有任务进行中")

    def save_snapshot(self):
        with maintenance_lock(self.state):
            return self.save_snapshot_unlocked()

    def save_snapshot_unlocked(self):
        self.environment()
        before_process = self.states().get("redis", {})
        if before_process.get("statename") != "RUNNING":
            raise ValueError("Redis 未运行")
        before = redis_stats()
        if before.get("loading") != 0 or before.get("rdb_bgsave_in_progress") != 0:
            raise ValueError("Redis 正在加载或保存，请稍后重试")
        last_save = before.get("rdb_last_save_time")
        if not isinstance(last_save, int) or last_save > time.time() + 2:
            raise ValueError("Redis 快照时间不可比较")
        # Ensure LASTSAVE can prove a new snapshot even when two requests
        # occur in one second; total task time remains bounded.
        while time.time() <= last_save + 1:
            time.sleep(0.1)
        started = time.time()
        self.bgsave()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            info = redis_stats()
            current = self.states().get("redis", {})
            if current != before_process:
                raise ValueError("Redis 在快照期间重启，无法确认保存结果")
            if info.get("rdb_bgsave_in_progress") == 0:
                if info.get("rdb_last_bgsave_status") != "ok":
                    raise ValueError("Redis 后台保存失败")
                saved = info.get("rdb_last_save_time", 0)
                if saved > last_save and saved >= int(started):
                    path = self.data / "redis_dns_v2.rdb"
                    if path.is_symlink() or not path.is_file() or path.stat().st_size < 9:
                        raise ValueError("快照报告成功，但默认 RDB 文件不可验证；请检查自定义 Redis 路径")
                    with path.open("rb") as stream:
                        header = stream.read(9)
                    if not header.startswith(b"REDIS") or not header[5:].isdigit():
                        raise ValueError("RDB 文件头无效")
                    return {"state": "saved", "saved_at": saved, "bytes": path.stat().st_size,
                            "changes_after_save": info.get("rdb_changes_since_last_save"),
                            "message": "Redis 快照已保存；仅覆盖 Redis 持久化数据，不包含其他内存缓存或查询历史"}
            time.sleep(0.5)
        raise ValueError("等待 Redis 后台保存超时；保存可能继续进行，请检查持久化状态")

    def execute(self, task):
        if task["action"] == "restart_dns_core":
            if os.environ.get("LOVESTDNS_SYSTEMD") != "1":
                raise ValueError("网页重启仅适用于原生服务")
            from native.restart import restart
            return restart(self)
        if task["action"] == "apply_custom_mod":
            return self.apply(task["expected_sha256"])
        if task["action"] == "save_redis_snapshot":
            return self.save_snapshot()
        raise ValueError("Unsupported maintenance action")
