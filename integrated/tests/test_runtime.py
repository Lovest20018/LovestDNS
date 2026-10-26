import json
from pathlib import Path

import pytest
from runtime import observer, resources, entrypoint, component


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
