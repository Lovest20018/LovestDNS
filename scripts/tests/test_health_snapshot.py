"""Optional Linux collector tested with synthetic Docker output, without Docker."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "paopaodns-health-snapshot.py"
spec = importlib.util.spec_from_file_location("health_snapshot", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def dig_output(records, flags="qr rd ra", status="NOERROR"):
    return {"exit": 0, "stdout": (
        f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: 1\n"
        f";; flags: {flags}; QUERY: 1, ANSWER: {len(records)}, AUTHORITY: 0, ADDITIONAL: 0\n"
        + "\n".join(records) + "\n;; Query time: 1 msec\n"), "stderr": ""}


@pytest.mark.parametrize("output,failed", [
    (dig_output(["example.com. 0 IN A 192.0.2.1"]), False),
    (dig_output(["example.com. 30 IN CNAME target.example.com.",
                 "target.example.com. 0 IN A 192.0.2.1"]), False),
    (dig_output(["example.com. 30 IN CNAME target.example.com."]), True),
    (dig_output(["other.example.com. 30 IN A 192.0.2.1"]), True),
    (dig_output(["example.com. 0 IN A 192.0.2.1"], flags="qr tc rd ra"), True),
    (dig_output([], status="SERVFAIL"), True),
    ({"exit": None, "stdout": "", "stderr": "Timeout"}, True),
])
def test_probe_distinguishes_target_records_and_incomplete_results(monkeypatch, output, failed):
    monkeypatch.setattr(collector, "run", lambda *args: output)
    result = collector.query("paopaodns", "example.com", "frontend", 53, 6)
    assert result["probe_failed"] is failed


@pytest.mark.parametrize("partial", [False, True])
def test_collector_preserves_partial_reports_and_optional_paths(monkeypatch, capsys, partial):
    info = {"State": {"Pid": 123, "Running": True}, "Config": {"Image": "test"},
            "NetworkSettings": {"Ports": {}}}

    def run(command, timeout=10):
        if command[1] == "inspect":
            return {"exit": 0, "stdout": json.dumps([info]), "stderr": ""}
        return dig_output([command[7] + ". 0 IN A 192.0.2.1"])

    args = [str(SCRIPT), "--interval", "0"]
    if partial:
        args += ["--recursive-port", "0", "--forward-port", "0"]
    monkeypatch.setattr(collector.sys, "platform", "linux")
    monkeypatch.setattr(collector.sys, "argv", args)
    monkeypatch.setattr(collector, "run", run)
    monkeypatch.setattr(collector, "stats", lambda *args: {"error": "Permission denied"} if partial
                        else {"total.num.queries": 1, "time.up": 5})
    monkeypatch.setattr(collector, "fingerprints", lambda root: {"config": {"error": "Permission denied"}}
                        if partial else {"config": "sha256"})
    assert collector.main() == (2 if partial else 0)
    report = json.loads(capsys.readouterr().out)
    assert report["partial"] is partial
    assert len(report["queries"]) == (3 if partial else 7)
    assert report["production_configs_unchanged"] is (None if partial else True)


@pytest.mark.parametrize("raw", ["error permission denied", "total.num.queries=nan\n"])
def test_missing_or_nonfinite_stats_are_explicit_errors(monkeypatch, raw):
    monkeypatch.setattr(collector, "control", lambda *args: raw)
    assert "error" in collector.stats("/proc/123/root", "/tmp/uc_raw.sock")
