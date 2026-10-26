"""Deterministic custom_mod compiler with explicit, bounded dependency validation."""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

MAX_BYTES = 2 * 1024 * 1024
MARKERS = ("zones_dns", "zones_seq", "zones_qname_top", "zones_qname_top6", "zones_qname_list", "swaps_match")
TEMPLATE_ENV = {"CNAUTO", "CNFALL", "IPV6", "CUSTOM_FORWARD", "CUSTOM_FORWARD_TTL", "AUTO_FORWARD",
                "AUTO_FORWARD_CHECK", "CN_TRACKER", "USE_HOSTS", "USE_MARK_DATA", "RULES_TTL", "HTTP_FILE",
                "ADDINFO", "SHUFFLE", "EXPIRED_FLUSH", "SOCKS5", "DNSPORT", "DNS_SERVERNAME", "SERVER_IP",
                "SAFEMODE", "UPDATE", "TZ"}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def read_bounded(path, limit=MAX_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"文件不存在、过大或为链接：{path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"文件过大：{path.name}")
    return data


def overrides(data):
    values = {}
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r'([_a-zA-Z0-9]+)="([^"\r\n]*)"', line)
        if not match:
            raise ValueError(f"custom_env.ini 第 {number} 行需要 KEY=\"VALUE\" 格式")
        # Match the upstream exporter: empty overrides are not exported.
        if match[2]:
            values[match[1]] = match[2].replace("'", "")
    return values


def _domain(value):
    try:
        ascii_name = value.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("域名无效") from exc
    if len(ascii_name) > 253 or not ascii_name or any(
        not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?", label)
        for label in ascii_name.split(".")):
        raise ValueError(f"域名无效：{value[:80]}")
    return ascii_name


class Compiler:
    def __init__(self, data_dir, template, env):
        self.data_dir = Path(data_dir).resolve()
        self.template = template
        self.env = env
        self.dependencies = {}
        self.dependency_bytes = 0

    def dependency(self, name, cidr=False):
        path = Path(name)
        # /data is the documented public prefix, mapped to a test root in tests.
        if not re.fullmatch(r"/data/[A-Za-z0-9_./-]+", name) or ".." in path.parts:
            raise ValueError("引用文件必须位于 /data 中")
        path = self.data_dir / name[len("/data/"):]
        if self.data_dir not in path.resolve().parents or any(p.is_symlink() for p in (path, *path.parents) if p != self.data_dir):
            raise ValueError("引用文件不得通过链接离开数据目录")
        data = read_bounded(path)
        if name not in self.dependencies:
            self.dependency_bytes += len(data)
            if len(self.dependencies) >= 32 or self.dependency_bytes > 8 * 1024 * 1024:
                raise ValueError("引用文件总数或总大小超过应用校验上限")
        text = data.decode("utf-8-sig")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if cidr:
                try:
                    ipaddress.ip_network(line, strict=False)
                except ValueError as exc:
                    raise ValueError(f"CIDR 文件含无效网段：{name}") from exc
            else:
                self.selectors(line, allow_file=False)
        self.dependencies[name] = hashlib.sha256(data).hexdigest()
        return name

    def selectors(self, value, allow_file=True):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError("域名匹配不能为空或过长")
        selectors = []
        for token in value.split():
            if token.startswith("/") and allow_file:
                selectors.append("&" + self.dependency(token))
            elif token.startswith("regexp:"):
                if not token[len("regexp:"):] or any(c in token for c in ('"', "'", "\x00")):
                    raise ValueError("正则匹配格式无效")
                # Go/RE2 compatibility is checked when the candidate core starts.
                selectors.append(token)
            elif token.startswith("keyword:"):
                if not re.fullmatch(r"keyword:[A-Za-z0-9_.-]+", token):
                    raise ValueError("关键字匹配格式无效")
                selectors.append(token)
            else:
                prefix, _, domain = token.partition(":")
                if _:
                    if prefix not in {"domain", "full"}:
                        raise ValueError("不支持的域名匹配类型")
                else:
                    prefix, domain = "domain", token
                selectors.append(prefix + ":" + _domain(domain))
        return " ".join(dict.fromkeys(selectors))

    def env_addresses(self, key):
        if not isinstance(key, str) or not ENV_KEY.fullmatch(key) or key in TEMPLATE_ENV:
            raise ValueError("Hosts/Swaps 的 env_key 必须是独立的地址变量")
        value = self.env.get(key, "")
        if not value or len(value) > 4096:
            raise ValueError(f"地址变量未配置或过长：{key}")
        try:
            for address in value.split():
                ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"地址变量含无效 IP：{key}") from exc
        return key

    @staticmethod
    def rows(config, key, allowed):
        rows = config.get(key, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError(f"{key} 必须是最多 256 项的列表")
        for row in rows:
            if not isinstance(row, dict) or not set(row).issubset(allowed):
                raise ValueError(f"{key} 含未知字段或无效条目")
        return rows

    def compile(self, content):
        if len(content) > 512 * 1024:
            raise ValueError("custom_mod.yaml 过大")
        try:
            config = yaml.safe_load(content.decode("utf-8-sig"))
        except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
            raise ValueError("custom_mod.yaml 格式无效") from exc
        config = {} if config is None else config
        if not isinstance(config, dict) or not set(config).issubset({"Zones", "Hosts", "Swaps"}):
            raise ValueError("custom_mod.yaml 顶层只支持 Zones、Hosts、Swaps")
        fragments = {marker: [] for marker in MARKERS}
        seen = {}
        for index, row in enumerate(self.rows(config, "Zones", {"zone", "dns", "ttl", "seq", "socks5"}), 1):
            selector = self.selectors(row.get("zone"))
            seq = row.get("seq", "top")
            ttl = row.get("ttl", 0)
            if not isinstance(seq, str) or seq not in {"top", "top6", "list"} or isinstance(ttl, bool) or not isinstance(ttl, int) or not 0 <= ttl <= 604800:
                raise ValueError("Zone 优先级或 TTL 无效")
            dns = row.get("dns")
            if not isinstance(dns, str) or not dns.strip():
                raise ValueError("Zone 需要 DNS 服务器")
            upstreams = []
            proxy = row.get("socks5", "no")
            if not isinstance(proxy, str) or proxy not in {"yes", "no"}:
                raise ValueError("socks5 只能是 yes 或 no")
            for address in dns.split(","):
                address = address.strip()
                parsed = urlsplit(address)
                try:
                    valid_port = parsed.port is None or 0 < parsed.port <= 65535
                    hostname = parsed.hostname
                except ValueError as exc:
                    raise ValueError("Zone DNS 端口无效") from exc
                if parsed.scheme not in {"udp", "tcp"} or not hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not valid_port:
                    raise ValueError("Zone DNS 仅支持 udp:// 或 tcp:// 地址")
                try:
                    ipaddress.ip_address(hostname)
                except ValueError:
                    _domain(hostname)
                if proxy == "yes" and parsed.scheme != "tcp":
                    raise ValueError("SOCKS5 的 Zone DNS 需要 TCP")
                upstreams.append({"addr": address})
            if len(upstreams) > 16:
                raise ValueError("单条 Zone 的 DNS 服务器过多")
            if (seq, selector) in seen:
                raise ValueError("同一优先级存在重复 Zone 匹配")
            seen[seq, selector] = True
            forward = f"suite_zone_{index}_forward"
            sequence = f"suite_zone_{index}"
            args = {"concurrent": 3, "allowcode": 23, "upstreams": upstreams}
            if proxy == "yes":
                socks = self.env.get("SOCKS5", "").lstrip("@")
                try:
                    parsed = urlsplit("socks5://" + socks)
                    if not parsed.hostname or not parsed.port or parsed.username or parsed.password:
                        raise ValueError()
                except ValueError as exc:
                    raise ValueError("SOCKS5 地址未配置或无效") from exc
                args["socks5"] = socks
            fragments["zones_dns"].append({"tag": forward, "type": "forward", "args": args})
            steps = [{"exec": "$" + forward}]
            if ttl:
                steps.append({"exec": f"ttl 0-{ttl}"})
            steps.append({"exec": "ok"})
            fragments["zones_seq"].append({"tag": sequence, "type": "sequence", "args": steps})
            fragments[f"zones_qname_{seq}"].append({"matches": "qname " + selector, "exec": "goto " + sequence})
        hosts = []
        for row in self.rows(config, "Hosts", {"env_key", "zone"}):
            key = self.env_addresses(row.get("env_key"))
            hosts.append({"matches": "qname " + self.selectors(row.get("zone")), "exec": "ip_hosts " + key})
        fragments["zones_qname_top"] = hosts + fragments["zones_qname_top"]
        cidr_owners = {}
        for row in self.rows(config, "Swaps", {"env_key", "cidr_file"}):
            key = self.env_addresses(row.get("env_key"))
            files = row.get("cidr_file")
            if isinstance(files, str):
                files = files.split()
            if not isinstance(files, list) or not 0 < len(files) <= 32 or any(not isinstance(f, str) for f in files):
                raise ValueError("Swaps 需要 CIDR 文件列表")
            names = []
            for name in files:
                self.dependency(name, cidr=True)
                if name in cidr_owners and cidr_owners[name] != key:
                    raise ValueError("同一 CIDR 文件不能绑定不同地址变量")
                cidr_owners[name] = key
                names.append("&" + name)
            fragments["swaps_match"].append({"matches": "resp_ip " + " ".join(names), "exec": "ip_rewrite " + key})
        text = self.template
        for marker, values in fragments.items():
            anchor = f"##{marker}_start##\n##{marker}_end##"
            if text.count(anchor) != 1:
                raise ValueError("当前 MosDNS 模板无法安全生成扩展配置，请检查版本兼容性")
            indent = 2 if marker in {"zones_dns", "zones_seq"} else 8
            block = yaml.safe_dump(values, allow_unicode=True, sort_keys=False) if values else ""
            block = "".join(" " * indent + line + "\n" for line in block.splitlines())
            text = text.replace(anchor, block)
        # Existing template plugins are retained. YAML loading detects malformed
        # generated structure; actual plugin/regex behavior requires a core start.
        result = yaml.safe_load(text)
        if len(text.encode()) > MAX_BYTES:
            raise ValueError("生成配置超过应用大小上限")
        if not isinstance(result, dict) or not isinstance(result.get("plugins"), list):
            raise ValueError("生成的 MosDNS 配置结构无效")
        tags = [p.get("tag") for p in result["plugins"] if isinstance(p, dict)]
        if len(tags) != len(set(tags)):
            raise ValueError("生成配置与现有插件标签冲突")
        return text


def prepare(data_dir, template_path, boot_environment):
    data_dir = Path(data_dir)
    raw_env = read_bounded(data_dir / "custom_env.ini", 256 * 1024)
    env = dict(boot_environment)
    values = overrides(raw_env)
    for key in TEMPLATE_ENV:
        if key in values and values[key] != boot_environment.get(key):
            raise ValueError(f"{key} 已改变，需重启容器重新生成模板后再应用")
    env.update(values)
    # Normalize checkout line endings before locating the generated-config
    # anchors. Windows fixtures and edited templates may contain CRLF even
    # though the compiler's anchors are expressed with LF.
    template = read_bounded(template_path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    raw_mod = read_bounded(data_dir / "custom_mod.yaml", 512 * 1024)
    compiler = Compiler(data_dir, template, env)
    compiled = compiler.compile(raw_mod)
    dependencies = {"custom_mod.yaml": hashlib.sha256(raw_mod).hexdigest(),
                    "custom_env.ini": hashlib.sha256(raw_env).hexdigest(),
                    "mosdns_base.yaml": hashlib.sha256(template.encode()).hexdigest(),
                    **compiler.dependencies}
    fingerprint = hashlib.sha256(json.dumps(dependencies, sort_keys=True).encode()).hexdigest()
    return {"sha256": fingerprint, "inputs": dependencies, "candidate": compiled, "environment": env}
