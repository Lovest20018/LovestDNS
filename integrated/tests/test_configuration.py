import json

import pytest
import yaml

from runtime.configuration import Compiler, MARKERS, prepare


def template():
    blocks = []
    for name in MARKERS[:2]:
        blocks.append(f"##{name}_start##\n##{name}_end##")
    blocks += ["  - tag: accept\n    type: sequence\n    args:", "##swaps_match_start##\n##swaps_match_end##", "        - exec: ok",
               "  - tag: main\n    type: sequence\n    args:",
               "##zones_qname_top_start##\n##zones_qname_top_end##",
               "##zones_qname_top6_start##\n##zones_qname_top6_end##",
               "##zones_qname_list_start##\n##zones_qname_list_end##", "        - exec: ok"]
    return "log:\n  level: error\nplugins:\n" + "\n".join(blocks) + "\n"


@pytest.fixture
def config_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    base = tmp_path / "mosdns_base.yaml"
    base.write_text(template(), encoding="utf-8")
    (data / "custom_env.ini").write_text('LOCAL_IP="192.0.2.10 2001:db8::1"\n')
    (data / "custom_mod.yaml").write_text("# empty custom configuration\n")
    return data, base


def test_zones_with_same_upstream_keep_sequence_and_stable_order(config_root):
    data, base = config_root
    content = {"Zones": [
        {"zone": "company.example", "dns": "udp://192.0.2.1:53", "seq": "list", "ttl": 30},
        {"zone": "company.example", "dns": "udp://192.0.2.1:53", "seq": "top", "ttl": 30},
    ], "Hosts": [{"env_key": "LOCAL_IP", "zone": "nas.example"}]}
    (data / "custom_mod.yaml").write_text(yaml.safe_dump(content))
    first = prepare(data, base, {"CNAUTO": "yes"})
    second = prepare(data, base, {"CNAUTO": "yes"})
    assert first["candidate"] == second["candidate"]
    assert first["sha256"] == second["sha256"]
    plugins = yaml.safe_load(first["candidate"])["plugins"]
    main = next(p for p in plugins if p["tag"] == "main")["args"]
    assert main[0]["exec"] == "ip_hosts LOCAL_IP"
    assert main[1]["exec"] == "goto suite_zone_2"
    assert main[2]["exec"] == "goto suite_zone_1"


def test_referenced_files_and_environment_are_bound_to_plan(config_root):
    data, base = config_root
    (data / "networks.txt").write_text("192.0.2.0/24\n")
    (data / "custom_mod.yaml").write_text('Swaps:\n- env_key: LOCAL_IP\n  cidr_file: ["/data/networks.txt"]\n')
    first = prepare(data, base, {"CNAUTO": "yes"})
    assert "/data/networks.txt" in first["inputs"]
    (data / "networks.txt").write_text("198.51.100.0/24\n")
    assert prepare(data, base, {"CNAUTO": "yes"})["sha256"] != first["sha256"]
    (data / "custom_env.ini").write_text('CNAUTO="no"\n')
    with pytest.raises(ValueError, match="需重启"):
        prepare(data, base, {"CNAUTO": "yes"})


@pytest.mark.parametrize("content", [
    {"Unknown": []}, {"Zones": "text"}, {"Zones": [{"zone": "x.example", "dns": "https://example.com"}]},
    {"Hosts": [{"env_key": "MISSING", "zone": "x.example"}]},
    {"Hosts": [{"env_key": "LOCAL_IP", "zone": "/etc/passwd"}]},
    {"Hosts": [{"env_key": "LOCAL_IP", "zone": "/data/../secrets"}]},
    {"Zones": [{"zone": "x.example", "dns": "udp://192.0.2.1:53", "ttl": True}]},
    {"Zones": [{"zone": "x.example", "dns": "tcp://192.0.2.1:99999"}]},
    {"Zones": [{"zone": "x.example", "dns": "udp://192.0.2.1:53", "socks5": "yes"}]},
    {"Zones": [{"zone": "x.example", "dns": "udp://192.0.2.1:53", "seq": []}]},
    {"Zones": [{"zone": "x.example", "dns": "udp://192.0.2.1:53", "extra": "ignored?"}]},
])
def test_invalid_mod_or_dependencies_are_rejected(config_root, content):
    data, base = config_root
    (data / "custom_mod.yaml").write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError):
        prepare(data, base, {"CNAUTO": "yes"})


def test_overlapping_cidr_file_assignment_is_rejected(config_root):
    data, base = config_root
    (data / "networks.txt").write_text("192.0.2.0/24\n")
    raw = {"Swaps": [{"env_key": key, "cidr_file": "/data/networks.txt"} for key in ("FIRST", "SECOND")]}
    with pytest.raises(ValueError, match="不同地址变量"):
        Compiler(data, template(), {"FIRST": "192.0.2.1", "SECOND": "192.0.2.2"}).compile(yaml.safe_dump(raw).encode())


def test_unknown_runtime_template_fails_closed(config_root):
    data, base = config_root
    base.write_text("plugins: []\n")
    with pytest.raises(ValueError, match="模板"):
        prepare(data, base, {"CNAUTO": "yes"})
