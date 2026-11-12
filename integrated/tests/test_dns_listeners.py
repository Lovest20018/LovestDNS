import json
from pathlib import Path

import pytest
from runtime import dns_listeners as dns


def row(local, remote='00000000:0000', state='07', inode='42'):
    return f'0: {local} {remote} {state} 0:0 0:0 0 0 0 {inode} 1'


def test_proc_socket_matching_excludes_upstream_and_other_processes():
    data = 'header\n' + '\n'.join([row('0100007F:14B5'), row('00000000:0035', inode='43'),
                                   row('0100007F:C000', state='01')])
    result = dns.socket_rows(data, {'42'}, 'udp', byteorder='little')
    assert result == [{'address': '127.0.0.1', 'port': 5301, 'transport': 'udp', 'family': 4, 'wildcard': False}]
    assert dns.socket_rows(data, {'42'}, 'tcp', byteorder='little') == []


@pytest.mark.parametrize('encoded,expected', [('00000000000000000000000001000000', '::1'),
                                             ('00000000000000000000000000000000', '::'),
                                             ('B80D0120000000000000000001000000', '2001:db8::1')])
def test_ipv6_proc_uses_native_32bit_words(encoded, expected):
    result = dns.socket_rows('header\n' + row(encoded + ':0035', state='0A'), {'42'}, 'tcp', True, 'little')
    assert result[0]['address'] == expected
    assert result[0]['wildcard'] == (expected == '::')


def test_process_identity_change_discards_entire_observation(tmp_path, monkeypatch):
    root = tmp_path / '12'
    (root / 'fd').mkdir(parents=True)
    (root / 'fd/1').touch()
    (root / 'net').mkdir()
    (root / 'net/udp').write_text('header\n' + row('00000000:0035'))
    (root / 'net/tcp').write_text('header\n' + row('00000000:0035', state='0A'))
    monkeypatch.setattr(dns.os, 'readlink', lambda _: 'socket:[42]')
    clock = iter([10, 11])
    monkeypatch.setattr(dns, 'process_start', lambda _: next(clock))
    state = {'statename': 'RUNNING', 'pid': 12}
    assert dns.observe_process(state, tmp_path) == {'available': False, 'reason': 'unconfirmed', 'endpoints': []}
    monkeypatch.setattr(dns, 'process_start', lambda _: 11)
    result = dns.observe_process(state, tmp_path)
    assert result['available'] and result['pid'] == 12
    assert {e['transport'] for e in result['endpoints']} == {'udp', 'tcp'}
    monkeypatch.setattr(dns, 'MAX_BYTES', 5)
    assert not dns.observe_process(state, tmp_path)['available']


def test_missing_proc_and_unknown_mode_do_not_invent_entry_or_leak_env(tmp_path):
    result = dns.summary({'WEB_UI_TOKEN': 'must-not-leak', 'SOCKS5': 'secret:123'},
                         {'mosdns': {'statename': 'RUNNING', 'pid': 12}}, tmp_path)
    assert result['entry_component'] is None and result['mode'] == 'unknown'
    assert result['listeners']['mosdns']['reason'] == 'unconfirmed'
    assert result['listeners']['unbound-raw']['reason'] == 'not_running'
    assert result['client_path_verified'] is False
    assert 'secret' not in json.dumps(result) and 'TOKEN' not in json.dumps(result)
    assert dns.summary({'CNAUTO': 'no'}, {}, tmp_path)['entry_component'] == 'unbound-raw'


def test_start_time_handles_spaces_and_parentheses_in_process_name(tmp_path):
    (tmp_path / 'stat').write_text('12 (a ) tricky (name)) ' + ' '.join(['S'] + ['0'] * 18 + ['1234'] + ['0'] * 8))
    assert dns.process_start(tmp_path) == 1234


def test_dns_config_matching_excludes_metrics_and_remote_control(tmp_path):
    (tmp_path / 'mosdns.yaml').write_text('api:\n  http: :9091\nplugins:\n- type: udp_server\n  args:\n    listen: ":53"\n- type: tcp_server\n  args:\n    listen: "[::]:53"\n')
    config = dns.configured_sockets('mosdns', tmp_path)
    assert dns.matches_config({'address': '::', 'port': 53, 'transport': 'udp', 'wildcard': True}, config)
    assert not dns.matches_config({'address': '::', 'port': 9091, 'transport': 'tcp', 'wildcard': True}, config)
    (tmp_path / 'unbound_raw.conf').write_text('server:\n    interface: 127.0.0.1\n    port: 15453\nremote-control:\n    control-interface: 127.0.0.1\n    control-port: 8953\n')
    config = dns.configured_sockets('unbound-raw', tmp_path)
    assert dns.matches_config({'address': '127.0.0.1', 'port': 15453, 'transport': 'tcp', 'wildcard': False}, config)
    assert not dns.matches_config({'address': '127.0.0.1', 'port': 8953, 'transport': 'tcp', 'wildcard': False}, config)


def test_missing_generated_config_prevents_dns_target_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(dns, 'observe_process', lambda *args: {'available': True, 'reason': None, 'endpoints': [
        {'address': '127.0.0.1', 'port': 9091, 'transport': 'tcp', 'family': 4, 'wildcard': False}]})
    result = dns.summary({'CNAUTO': 'yes'}, {}, tmp_path, tmp_path)
    assert not result['listeners']['mosdns']['available']
    assert not result['listeners']['mosdns']['endpoints']


def test_nonliteral_listeners_remain_unconfirmed_not_empty_observation(tmp_path, monkeypatch):
    (tmp_path / 'mosdns.yaml').write_text('plugins:\n- type: udp_server\n  args:\n    listen: "${DNS_LISTEN}"\n')
    monkeypatch.setattr(dns, 'observe_process', lambda *args: {'available': True, 'reason': None, 'endpoints': [
        {'address': '127.0.0.1', 'port': 53, 'transport': 'udp', 'family': 4, 'wildcard': False}]})
    result = dns.summary({'CNAUTO': 'yes'}, {}, tmp_path, tmp_path)
    assert result['listeners']['mosdns'] == {'available': False, 'reason': 'unconfirmed', 'endpoints': []}
