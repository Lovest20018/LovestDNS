"""Bounded Linux process-socket observations, never host NAT or client-path claims."""
import ipaddress
import os
from pathlib import Path
import re
import sys
import yaml

COMPONENTS = ('mosdns', 'unbound-raw', 'unbound-forward')
MAX_BYTES = 512 * 1024
MAX_FDS = 4096


def read_text(path):
    with path.open('rb') as source:
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('observation_limit')
    return data.decode('utf-8')


def configured_sockets(component, directory):
    """Identify DNS listeners in generated files; omit unknown/custom syntax.

    An owned socket alone may be MosDNS's HTTP metrics listener. Matching these
    files classifies its purpose, not whether every rule was loaded/applied.
    """
    if component == 'mosdns':
        config = yaml.safe_load(read_text(directory / 'mosdns.yaml'))
        plugins = config.get('plugins') if isinstance(config, dict) else None
        if not isinstance(plugins, list) or len(plugins) > 2048:
            raise ValueError('unknown_mosdns_config')
        result = []
        for plugin in plugins:
            if not isinstance(plugin, dict) or plugin.get('type') not in {'udp_server', 'tcp_server'}:
                continue
            args = plugin.get('args')
            listen = args.get('listen') if isinstance(args, dict) else None
            if not isinstance(listen, str):
                continue
            match = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[0-9.]*):(\d+)', listen)
            if match:
                address = match[1].strip('[]')
                if address:
                    address = str(ipaddress.ip_address(address))
                result.append((plugin['type'].split('_')[0], address, int(match[2])))
        return result
    role = {'unbound-raw': 'raw', 'unbound-forward': 'forward'}[component]
    text = read_text(directory / f'unbound_{role}.conf')
    section, port, interfaces = '', None, []
    for line in text.splitlines():
        match = re.match(r'^\s*([\w-]+):\s*(.*?)\s*(?:#.*)?$', line)
        if not match:
            continue
        key, value = match.groups()
        if not value:
            section = key
        elif section == 'server':
            value = value.strip('\"\'')
            if key == 'port' and value.isdecimal():
                port = int(value)
            elif key == 'interface':
                address, _, explicit = value.partition('@')
                interfaces.append((str(ipaddress.ip_address(address)), int(explicit) if explicit else None))
    return [(transport, address, explicit or port) for address, explicit in interfaces
            for transport in ('udp', 'tcp') if explicit or port]


def matches_config(endpoint, configured):
    for transport, address, port in configured:
        if transport != endpoint['transport'] or port != endpoint['port']:
            continue
        if not address or address == endpoint['address']:
            return True
        if ipaddress.ip_address(address).is_unspecified and endpoint['wildcard']:
            return True  # A dual-stack wildcard can appear as one IPv6 socket.
    return False


def process_start(path):
    # comm (field 2) can contain spaces and parentheses; starttime is field 22.
    fields = read_text(path / 'stat').rsplit(')', 1)[1].split()
    return int(fields[19])


def socket_rows(text, owned, transport, ipv6=False, byteorder=sys.byteorder):
    result = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[9] not in owned:
            continue
        if fields[3] != ('0A' if transport == 'tcp' else '07'):
            continue  # TCP LISTEN / unconnected UDP only, not upstream sockets.
        address, raw_port = fields[1].split(':')
        expected = 32 if ipv6 else 8
        if len(address) != expected:
            raise ValueError('invalid_socket_address')
        packed = b''.join(int(address[i:i + 8], 16).to_bytes(4, byteorder) for i in range(0, expected, 8))
        port = int(raw_port, 16)
        if not 0 < port <= 65535:
            continue
        ip = ipaddress.ip_address(packed)
        result.append({'address': str(ip), 'port': port, 'transport': transport,
                       'family': 6 if ipv6 else 4, 'wildcard': ip.is_unspecified})
    return result


def observe_process(state, proc=Path('/proc')):
    pid = state.get('pid')
    if state.get('statename') != 'RUNNING' or type(pid) is not int or pid <= 0:
        return {'available': False, 'reason': 'not_running', 'endpoints': []}
    root = proc / str(pid)
    try:
        before = process_start(root)
        owned = set()
        with os.scandir(root / 'fd') as descriptors:
            for count, descriptor in enumerate(descriptors, 1):
                if count > MAX_FDS:
                    raise ValueError('observation_limit')
                try:
                    target = os.readlink(descriptor.path)
                except FileNotFoundError:
                    continue  # A descriptor can close while the collector reads it.
                match = re.fullmatch(r'socket:\[(\d+)\]', target)
                if match:
                    owned.add(match[1])
        endpoints = []
        for name, transport, ipv6 in [('udp', 'udp', False), ('tcp', 'tcp', False),
                                      ('udp6', 'udp', True), ('tcp6', 'tcp', True)]:
            try:
                value = read_text(root / 'net' / name)
            except FileNotFoundError:
                if ipv6:
                    continue  # IPv6 tables need not exist on this kernel.
                raise
            endpoints.extend(socket_rows(value, owned, transport, ipv6))
        if process_start(root) != before:
            raise ValueError('process_changed')
        unique = {(e['address'], e['port'], e['transport']): e for e in endpoints}
        if len(unique) > 64:
            raise ValueError('observation_limit')
        return {'available': True, 'reason': None, 'pid': pid, 'start_ticks': str(before),
                'endpoints': [unique[key] for key in sorted(unique)]}
    except (OSError, ValueError, IndexError, UnicodeError):
        # Do not leak filesystem paths or turn one missing observation into a
        # failure of component health, counters, or the DNS data plane.
        return {'available': False, 'reason': 'unconfirmed', 'endpoints': []}


def summary(env, states, proc=Path('/proc'), directory=Path('/tmp')):
    mode = {'yes': 'automatic', 'no': 'manual'}.get(env.get('CNAUTO'), 'unknown')
    listeners = {}
    for name in COMPONENTS:
        value = observe_process(states.get(name, {}), proc)
        if value['available']:
            try:
                configured = configured_sockets(name, directory)
                if not configured:
                    raise ValueError('unknown_dns_listeners')
                value['endpoints'] = [endpoint for endpoint in value['endpoints'] if matches_config(endpoint, configured)]
            except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
                value = {'available': False, 'reason': 'unconfirmed', 'endpoints': []}
        listeners[name] = value
    return {'source': 'linux_process_sockets', 'scope': 'service_network_namespace',
            'mode': mode, 'entry_component': {'automatic': 'mosdns', 'manual': 'unbound-raw'}.get(mode),
            'client_path_verified': False,
            'listeners': listeners}
