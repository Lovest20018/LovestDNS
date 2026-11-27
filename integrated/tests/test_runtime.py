from contextlib import ExitStack
import json
from pathlib import Path
import socket
import socketserver
import threading

import pytest
from runtime import observer, resources, entrypoint, component


@pytest.mark.parametrize('nonfinite', [False, True])
def test_collector_publishes_serialized_snapshot(monkeypatch, nonfinite):
    sample = {'schema_version': 1, 'records': [float('nan') if nonfinite else 7], '_internal': 'private'}
    monkeypatch.setattr(observer, 'collect', lambda _: sample)

    def stop(_):
        raise StopIteration

    monkeypatch.setattr(observer.time, 'sleep', stop)
    collector = observer.Collector()
    assert json.loads(collector.snapshot_bytes) == {'error': 'Collector starting'}
    with pytest.raises(StopIteration):
        collector.run()
    assert collector.snapshot_bytes == json.dumps(collector.snapshot, allow_nan=False).encode() + b'\n'
    assert collector.snapshot['schema_version'] == 1
    if nonfinite:
        assert 'error' in collector.snapshot
    else:
        assert collector.snapshot == {'schema_version': 1, 'records': [7]}


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='Unix sockets unavailable')
def test_observer_serves_fast_clients_during_slow_reads_and_bounds_workers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(observer, 'RUN_DIR', Path('.'))
    sample = {'schema_version': 1, 'sampled_at': 100, 'ready': True}
    published = threading.Event()

    def collect_once(collector):
        collector._publish(sample)
        published.set()

    monkeypatch.setattr(observer.Collector, 'run', collect_once)
    servers = []
    serve = socketserver.BaseServer.serve_forever

    def capture_server(server):
        servers.append(server)

    monkeypatch.setattr(socketserver.BaseServer, 'serve_forever', capture_server)
    observer.main()
    assert published.wait(2)
    server, = servers
    handler = server.RequestHandlerClass
    handle = handler.handle
    slow_reads = threading.Semaphore(0)
    finished = threading.Event()
    request_thread = server.process_request_thread

    def observe_reads(request_handler):
        slow_reads.release()
        handle(request_handler)

    def observe_completion(*args):
        request_thread(*args)
        finished.set()

    monkeypatch.setattr(handler, 'handle', observe_reads)
    monkeypatch.setattr(server, 'process_request_thread', observe_completion)
    worker = threading.Thread(target=serve, args=(server, .01), daemon=True)
    worker.start()
    try:
        with ExitStack() as clients:
            def connect():
                connection = clients.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                connection.settimeout(1)
                connection.connect('observer.sock')
                return connection

            slow = connect()
            slow.sendall(b'snap')
            assert slow_reads.acquire(timeout=1)
            with connect() as fast:
                fast.sendall(b'snapshot\n')
                with fast.makefile('rb') as output:
                    assert json.loads(output.readline()) == sample
                    assert output.read() == b''
            assert slow_reads.acquire(timeout=1)
            assert finished.wait(1)

            for _ in range(7):
                connect().sendall(b'snap')
                assert slow_reads.acquire(timeout=1)
            with connect().makefile('rb') as output:
                assert output.readline() == b'{"error":"Collector busy"}\n'
                assert output.read() == b''
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_rate_requires_same_process_and_monotonic_counters():
    first = {"total.num.queries": 10, "total.num.cachehits": 5, "total.num.cachemiss": 5, "time.up": 10}
    last = {"total.num.queries": 20, "total.num.cachehits": 13, "total.num.cachemiss": 7, "time.up": 15}
    assert observer.rate(first, last, 5, True) == {"comparable": True, "qps": 2.0, "cache_hit_ratio": 0.8,
                "comparison_reason": "comparable", "window_seconds": 5}
    assert observer.rate(first, last, 5, False)["comparison_reason"] == "process_changed"
    assert observer.rate(last, first, 5, True)["comparison_reason"] == "counters_reset"
    assert observer.rate(first, last, 5, False)["qps"] is None
    assert observer.rate(last, first, 5, True)["comparable"] is False
    assert observer.rate(None, last, 5, True)["comparable"] is False
    assert observer.rate(first, last, float('nan'), True)["comparison_reason"] == 'clock_changed'


def test_parse_counters_rejects_nonfinite_and_preserves_missing():
    assert observer.parse_unbound("total.num.queries=0\ntime.up=1") == {"total.num.queries": 0, "time.up": 1}
    for text in ("total.num.queries=NaN\ntime.up=1", "total.num.queries=-1\ntime.up=1", "total.num.queries=1"):
        with pytest.raises(ValueError):
            observer.parse_unbound(text)


def test_redis_reports_persistence_and_separate_databases():
    result = observer.parse_redis("loading:0\r\nused_memory:100\r\ndb0:keys=4,expires=0,avg_ttl=0\r\ndb1:keys=9,expires=0,avg_ttl=0\r\nrdb_last_bgsave_status:err\r\n")
    assert result["db0"]["keys"] == 4
    assert result["db1"]["keys"] == 9
    assert result["rdb_last_bgsave_status"] == "err"
    assert "keyspace_hits" not in result


def test_cgroup_limit_and_web_reserve(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemAvailable: 8388608 kB\n")
    limit = tmp_path / "memory.max"
    limit.write_text(str(512 * 1024 * 1024))
    assert resources.memory_budget(info, [str(limit)]) == 384
    limit.write_text("max")
    assert resources.memory_budget(info, [str(limit)]) == 6553


@pytest.mark.parametrize('limit_name,usage_name', [('memory.max', 'memory.current'), ('memory.limit_in_bytes', 'memory.usage_in_bytes')])
def test_cache_budget_subtracts_usage_and_rejects_exhausted_headroom(tmp_path, limit_name, usage_name):
    info = tmp_path / 'meminfo'
    info.write_text('MemAvailable: 8388608 kB\n')
    limit, usage = tmp_path / limit_name, tmp_path / usage_name
    limit.write_text(str(512 * 1024 * 1024))
    usage.write_text(str(100 * 1024 * 1024))
    assert resources.memory_budget(info, [limit]) == 284
    usage.write_text(str(500 * 1024 * 1024))
    with pytest.raises(ValueError, match='headroom'):
        resources.memory_budget(info, [limit])


def test_cgroup_discovery_includes_parent_slice(tmp_path):
    membership = tmp_path / 'membership'
    membership.write_text('0::/system.slice/test.service\n')
    root = tmp_path / 'cgroup'
    assert resources.cgroup_limits(root, membership) == [root/'memory.max', root/'memory/memory.limit_in_bytes',
        root/'system.slice/test.service/memory.max', root/'system.slice/memory.max']


def test_migration_preserves_inputs_and_does_not_touch_cache(tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir(); state.mkdir()
    (data / "mosdns.yaml").write_text("custom template")
    (data / "redis_dns_v2.rdb").write_bytes(b"REDIS-cache-test")
    entrypoint.preserve_inputs(data, state)
    (data / "mosdns.yaml").write_text("modified later")
    entrypoint.preserve_inputs(data, state)
    assert (state / "initial-inputs/mosdns.yaml").read_text() == "custom template"
    assert (data / "redis_dns_v2.rdb").read_bytes() == b"REDIS-cache-test"
    manifest = json.loads((state / "initial-inputs.json").read_text())
    assert manifest["files"]["redis_dns_v2.rdb"]["copied"] is False


def test_existing_unbound_template_gets_private_distinct_control_socket(tmp_path):
    path = tmp_path / "unbound_forward.conf"
    path.write_text('server:\n    port: 5304\nremote-control:\n    control-enable: no\n    control-interface: 0.0.0.0\ncachedb:\n    redis-logical-db: 1\n')
    component.prepare_unbound("forward", tmp_path)
    text = path.read_text()
    assert "control-enable: no" not in text
    assert "control-interface: 0.0.0.0" not in text
    assert "control-interface: /tmp/uc_forward.sock" in text
    assert "redis-logical-db: 1" in text
    assert text.count("remote-control:") == 1
    component.prepare_unbound("forward", tmp_path)
    assert path.read_text().count("control-interface:") == 1


def test_component_overrides_are_data_not_shell(tmp_path):
    path = tmp_path / "custom_env.ini"
    path.write_text('VALUE="$(echo literal)"\nBAD="bad" trailing\n#COMMENT="ignored"\n')
    assert component.read_overrides(path) == {"VALUE": "$(echo literal)"}


def test_dnscrypt_upgrade_preserves_protocol_and_explicit_user_choice(tmp_path, monkeypatch):
    info, config = tmp_path / 'build-info.json', tmp_path / 'dnscrypt.toml'
    monkeypatch.setenv('SUITE_BUILD_INFO', str(info))
    info.write_text(json.dumps({'components': {'selected': ['dnscrypt']}}))
    original = 'http3 = false\n[static.fixture]\nstamp = "fixture"\n'
    config.write_text(original)
    component.preserve_dnscrypt_defaults(config)
    assert component.tomllib.loads(config.read_text())['pqdnscrypt'] is False
    assert component.tomllib.loads(config.read_text())['lb_strategy'] == 'p2'
    value = config.read_text()
    component.preserve_dnscrypt_defaults(config)
    assert config.read_text() == value
    config.write_text('pqdnscrypt = true\nlb_strategy = "first"\n' + original)
    component.preserve_dnscrypt_defaults(config)
    assert component.tomllib.loads(config.read_text())['pqdnscrypt'] is True
    assert component.tomllib.loads(config.read_text())['lb_strategy'] == 'first'
    info.write_text(json.dumps({'components': {'selected': ['redis']}}))
    config.write_text(original)
    component.preserve_dnscrypt_defaults(config)
    assert config.read_text() == original


def test_collector_rate_uses_monotonic_clock_when_wall_clock_moves_back(tmp_path, monkeypatch):
    ticks, walls = iter([100, 105]), iter([1000, 900])
    monkeypatch.setattr(observer.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(observer.time, 'time', lambda: next(walls))
    monkeypatch.setattr(observer, 'RUN_DIR', tmp_path)
    (tmp_path / 'ready').touch()
    monkeypatch.setattr(observer, 'read_boot_env', lambda: {'CNAUTO': 'yes'})
    monkeypatch.setattr(observer, 'process_status', lambda: {name: {'statename': 'RUNNING', 'pid': 1, 'start': 1} for name in ['redis', 'unbound-raw', 'unbound-forward', 'mosdns', 'dnscrypt', 'watcher']})
    monkeypatch.setattr(observer, 'redis_stats', lambda: {})
    counter = {'queries': 10}
    monkeypatch.setattr(observer, 'unbound_stats', lambda path: {'total.num.queries': counter['queries'], 'total.num.cachehits': 0, 'total.num.cachemiss': counter['queries'], 'time.up': counter['queries']})
    first = observer.collect()
    counter['queries'] = 20
    second = observer.collect(first)
    assert second['sampled_at'] < first['sampled_at']
    assert second['layers']['recursive']['qps'] == 2
    assert second['layers']['recursive']['window_seconds'] == 5
