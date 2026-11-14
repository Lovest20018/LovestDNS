"""Native rule updater regressions: corrupt downloads cannot replace live rules."""
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import lzma
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "integrated"))
    source = ROOT / "packaging/native/runtime"
    for name in tuple(sys.modules):
        if name == "native" or name.startswith("native."):
            monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location("native", source / "__init__.py",
                                                submodule_search_locations=[str(source)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "native", module)
    spec.loader.exec_module(module)
    from native import rule_updates
    return rule_updates


@pytest.fixture
def core(tmp_path):
    value = SimpleNamespace(**{name: tmp_path / name for name in ("data", "state", "run", "temporary")})
    for name in ("data", "state", "run", "temporary"):
        getattr(value, name).mkdir()
    (value.run / "ready").touch()
    return value


def marker(data):
    compressed = lzma.compress(data)
    # Production MosDNS uses an XZ stream followed by a NUL-padded digest.
    return compressed + hashlib.sha512(compressed).hexdigest().encode().ljust(1024, b"\0")


def test_checksums_require_an_actual_digest_and_reject_empty_or_html(rules):
    for value in (b"", b"\n", b"<html>not available</html>", b"g" * 64):
        with pytest.raises(ValueError):
            rules.checksum(value, "sha256")
    assert rules.checksum(b"a" * 64 + b"  file\n", "sha256") == "a" * 64


def test_official_api_fallback_rejects_bad_primary_bytes(rules, tmp_path):
    old = tmp_path / "old"
    old.write_bytes(b"current rules")
    expected = b"verified next rules"
    calls = []
    def download(url, limit, deadline, home, env):
        calls.append(url)
        if "raw.githubusercontent.com" in url:
            raise OSError("connection reset")
        if "sha256sum" in url:
            return hashlib.sha256(expected).hexdigest().encode()
        return b"wrong bytes" if "cdn.jsdelivr.net" in url else expected
    value, receipt = rules.verified_asset("country", old, tmp_path, {}, 123, download)
    assert value == expected and receipt["source"].startswith("https://api.github.com/")
    assert old.read_bytes() == b"current rules"
    assert len(calls) == 5


def test_all_bad_downloads_keep_original_and_fail(rules, tmp_path):
    path = tmp_path / "active"
    path.write_bytes(b"previous")
    def bad(url, *args):
        return b"a" * 64 if "sha256sum" in url else b"bad download"
    with pytest.raises(rules.RuleUpdateFailed):
        rules.verified_asset("country", path, tmp_path, {}, 123, bad)
    assert path.read_bytes() == b"previous"


def test_compressed_rules_are_bounded_and_have_a_complete_stream(rules, monkeypatch):
    assert rules.expand(lzma.compress(b"valid")) == b"valid"
    with pytest.raises(ValueError):
        rules.expand(lzma.compress(b"valid") + b"trailer")
    monkeypatch.setattr(rules, "EXPANDED_LIMIT", 100)
    with pytest.raises(ValueError):
        rules.expand(lzma.compress(b"x" * 101))


def test_country_and_trackers_prepare_only_writable_files_and_preserve_additions(rules, core):
    (core.data / "trackerslist.txt").write_bytes(b"udp://user.example:80/announce\n")
    geo = b"fixture mmdb \xab\xcd\xefMaxMind.com"
    values = {"country": lzma.compress(geo), "trackers": lzma.compress(b"https://tracker.example/announce\n")}
    desired = rules.desired_files(values, core)
    assert desired[core.data / "Country-only-cn-private.mmdb"] == geo
    assert b"udp://user.example:80/announce\n" in desired[core.data / "trackerslist.txt"]
    assert all(core.data in path.parents or core.temporary in path.parents for path in desired)
    assert not (core.data / "Country-only-cn-private.mmdb").exists()


def test_global_marker_verifies_actual_embedded_trailer_before_publish(rules, core):
    # Keep the compressed stream large enough to satisfy the upstream guard.
    import random
    data = random.Random(0).randbytes(20000)
    value = marker(data)
    assert rules.desired_files({"global_mark": value}, core)[core.data / "global_mark.dat"] == value
    with pytest.raises(ValueError, match="checksum"):
        rules.desired_files({"global_mark": value[:-1024] + b"0" * 1024}, core)


def test_fetch_failure_writes_retry_state_without_changing_any_live_rule(rules, core, tmp_path):
    home = tmp_path / "immutable"
    home.mkdir()
    (home / "named.cache").write_bytes(b"bundled")
    (core.temporary / "named.cache").write_bytes(b"active")
    def failed(*args):
        raise OSError("offline")
    with pytest.raises(rules.RuleUpdateFailed):
        rules.update(home, {"CNAUTO": "no", "UPDATE": "weekly"}, actions=core, downloader=failed)
    status = rules.load_status(core.state)
    assert status["phase"] == "failed" and status["consecutive_failures"] == 1
    assert 59 <= status["next_attempt_at"] - status["completed_at"] <= 61
    assert not (core.state / "last-periodic-update").exists()
    assert (home / "named.cache").read_bytes() == b"bundled"
    assert (core.temporary / "named.cache").read_bytes() == b"active"


def test_apply_rearms_watcher_after_atomic_publish(rules, core, monkeypatch, tmp_path):
    target = core.data / "Country-only-cn-private.mmdb"
    target.write_bytes(b"previous")
    calls = []
    core.environment = lambda: {}
    core.states = lambda: {"watcher": {"statename": "RUNNING"}}
    core.control = lambda action, name: calls.append((action, name))
    core.wait_running = lambda name: calls.append(("wait", name))
    core.probe = lambda: calls.append(("probe",))
    monkeypatch.setattr(rules, "maintenance_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(rules, "runtime_backup", lambda core: SimpleNamespace(restore=lambda: calls.append(("restore",))))
    monkeypatch.setattr(rules, "desired_files", lambda *args: {target: b"next"})
    def run(*args, **kwargs):
        assert target.read_bytes() == b"next"
        assert calls == [("stop", "watcher")]
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(rules.subprocess, "run", run)
    assert rules.apply(core, tmp_path, {"CNAUTO": "yes"}, {}) == [str(target)]
    assert calls[0] == ("stop", "watcher")
    assert calls[-2:] == [("start", "watcher"), ("wait", "watcher")]


def test_failed_apply_restores_live_and_runtime_files_and_watcher(rules, core, monkeypatch, tmp_path):
    target = core.data / "Country-only-cn-private.mmdb"
    target.write_bytes(b"previous")
    calls = []
    core.environment = lambda: {}
    core.states = lambda: {"watcher": {"statename": "RUNNING"}}
    core.control = lambda action, name: calls.append((action, name))
    core.wait_running = lambda name: None
    core.probe = lambda: None
    monkeypatch.setattr(rules, "maintenance_lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(rules, "runtime_backup", lambda core: SimpleNamespace(restore=lambda: calls.append(("restore",))))
    monkeypatch.setattr(rules, "desired_files", lambda *args: {target: b"next"})
    def failed(*args, **kwargs):
        raise OSError("reload failed")
    monkeypatch.setattr(rules.subprocess, "run", failed)
    from native import restart
    monkeypatch.setattr(restart, "control", lambda *args: None)
    with pytest.raises(rules.RuleUpdateFailed, match="inspect recovery") as error:
        rules.apply(core, tmp_path, {"CNAUTO": "yes"}, {})
    assert error.value.recovery == {"attempted": True, "files_restored": True,
                                    "runtime_restored": True, "service_responded": True}
    assert target.read_bytes() == b"previous"
    assert ("restore",) in calls
    assert calls[-1] == ("start", "watcher")


def test_success_records_period_and_does_not_repeat_on_next_scheduler_tick(rules, core, monkeypatch, tmp_path):
    home = tmp_path / "immutable"
    home.mkdir()
    hints = b". 3600 IN NS A.ROOT-SERVERS.NET.\n"
    (home / "named.cache").write_bytes(hints)
    def download(url, *args):
        return hashlib.md5(hints).hexdigest().encode() if url.endswith(".md5") else hints
    monkeypatch.setattr(rules, "apply", lambda *args: [])
    result = rules.update(home, {"CNAUTO": "no", "UPDATE": "weekly"}, actions=core, downloader=download)
    assert result["phase"] == "succeeded"
    assert rules.next_attempt(core.state, 7 * 86400, result["completed_at"] + 30) == result["completed_at"] + 7 * 86400


def test_retry_state_survives_process_restart_and_caps_backoff(rules, core):
    rules.write(core.state / "last-periodic-update", b"1000")
    rules.write(core.state / rules.STATUS_NAME, json.dumps({
        "schema_version": 1, "phase": "failed", "next_attempt_at": 1100}).encode())
    assert rules.next_attempt(core.state, 7 * 86400, 1050) == 1100
    rules.write(core.state / rules.STATUS_NAME, json.dumps({
        "schema_version": 1, "phase": "running", "started_at": 1000}).encode())
    assert rules.next_attempt(core.state, 7 * 86400, 1050) < 1500
