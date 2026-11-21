"""Receive whole MosDNS transactions over the private recorder socket."""
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time

from runtime.dns_filter import ID, REVISION, core_call
from runtime.query_wire import RCODES, TYPES


def address(value):
    host, port = value.rsplit(':', 1)
    ip = ipaddress.ip_address(host.strip('[]').split('%', 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('invalid_port')
    return str(ip), port


def decode(value):
    epoch, sequence = value['epoch'], value['sequence']
    if not ID.fullmatch(epoch) or type(sequence) is not int or not 0 < sequence < 2 ** 64:
        raise ValueError('invalid_request_id')
    client, source_port = address(value['client'])
    entry, entry_port = address(value['entry'])
    started, finished = value['started_us'] / 1e6, value['finished_us'] / 1e6
    elapsed = value['elapsed_ms']
    if not math.isfinite(elapsed) or not 0 <= elapsed <= 30000 or not abs(time.time() - finished) <= 30:
        raise ValueError('invalid_transaction_time')
    name, qtype, qclass = value['name'], value['qtype'], value['qclass']
    if (not isinstance(name, str) or not name.endswith('.') or len(name) > 1021
            or not 0 <= qtype <= 65535 or not 0 <= qclass <= 65535 or value['transport'] not in ('udp', 'tcp')):
        raise ValueError('invalid_question')
    if abs((finished - started) * 1000 - elapsed) > 100:
        raise ValueError('transaction_clock_changed')
    code = value['rcode']
    if code is not None and (type(code) is not int or not 0 <= code <= 4095):
        raise ValueError('invalid_dns_rcode')
    addresses = value['addresses']
    if not isinstance(addresses, list) or len(addresses) > 4:
        raise ValueError('invalid_answer_addresses')
    result = {'id': f'mosdns:{epoch}:{sequence}', 'observed_at': started, 'response_at': finished if code is not None else None,
              'source_ip': client, 'source_port': source_port, 'entry_address': entry, 'entry_port': entry_port,
              'component': 'mosdns', 'transport': value['transport'], 'domain': name,
              'qtype': qtype, 'qclass': qclass, 'record_type': TYPES.get(qtype, str(qtype)),
              'same_key_packets': 1, 'outcome': 'response_observed' if code is not None else 'no_response_observed',
              'rcode': RCODES.get(code, str(code)) if code is not None else None, 'rcode_value': code,
              'answer_count': value['answer_count'] if code is not None else None,
              'addresses': [str(ipaddress.ip_address(ip)) for ip in addresses],
              'addresses_complete': value['addresses_complete'], 'truncated': value['truncated'] if code is not None else None,
              'elapsed_ms': round(elapsed, 3) if code is not None else None, 'branch': None, 'cache_hit': None,
              'timing_scope': 'mosdns_entry_processing'}
    verdict = value.get('filter')
    if verdict is not None:
        action = verdict['action']
        if action not in ('block', 'allow', 'pass') or not REVISION.fullmatch(verdict['revision']):
            raise ValueError('invalid_filter_decision')
        if action == 'block' and code not in (None, 5):
            raise ValueError('filter_refusal_mismatch')
        decision = {'action': action, 'revision': verdict['revision']}
        if action != 'pass':
            if not re.fullmatch('[a-f0-9]{24}', verdict['id']) or not 0 < len(verdict['source']) <= 64 or not 0 < len(verdict['rule']) <= 512:
                raise ValueError('invalid_filter_rule')
            decision.update(id=verdict['id'], source=verdict['source'], rule=verdict['rule'])
            if 'source_id' in verdict:
                if not isinstance(verdict['source_id'], str) or not ID.fullmatch(verdict['source_id']):
                    raise ValueError('invalid_filter_source')
                decision['source_id'] = verdict['source_id']
            if verdict.get('until'):
                decision['until'] = verdict['until']
        result['filter'] = decision
    return result


class CoreEvents:
    def __init__(self, recorder):
        self.recorder = recorder
        self.socket = Path(recorder.run) / 'filter-events.sock'
        self.control = Path(recorder.run) / 'dns-filter.sock'
        self.session = None
        self.losses = 0
        self.epoch = None
        self.listener = None

    def renew(self, endpoints, generation):
        if not self.listener or not endpoints or endpoints[0]['component'] != 'mosdns' or not self.control.exists():
            self.session = None
            return None
        pid = endpoints[0]['pid']
        previous = self.session
        token = previous[2] if previous and previous[:2] == (generation, pid) else os.urandom(16).hex()
        try:
            reply = core_call(self.recorder.run, 'record', token=token)
            if reply.get('pid') != pid or not ID.fullmatch(reply.get('epoch', '')):
                raise ValueError('filter_event_owner_changed')
        except (OSError, ValueError):
            self.session = None
            raise
        session = generation, pid, token, reply['epoch']
        with self.recorder.lock:
            if self.recorder.generation != generation or not self.recorder.enabled:
                return None
            if self.epoch == reply['epoch']:
                self.recorder.counters['core_event_drops'] += max(0, reply['drops'] - self.losses)
            self.losses = reply['drops']
            self.epoch = reply['epoch']
            self.session = session
        return reply['epoch']

    def start(self):
        if not hasattr(socket, 'AF_UNIX'):
            return
        self.socket.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket))
            self.socket.chmod(0o600)
            listener.listen(2)
            self.listener = listener
        except OSError:
            listener.close()
            return
        threading.Thread(target=self.receive, daemon=True).start()

    def receive(self):
        while not self.recorder.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            try:
                with connection, connection.makefile('rb') as stream:
                    while raw := stream.readline(4097):
                        if len(raw) > 4096 or not raw.endswith(b'\n'):
                            break
                        self.ingest(json.loads(raw))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.recorder.counters['core_event_drops'] += 1

    def ingest(self, value):
        session = self.session
        if session is None or value.get('token') != session[2] or value.get('epoch') != session[3]:
            return
        event = decode(value)
        recorder = self.recorder
        with recorder.lock:
            if session == self.session and recorder.enabled and recorder.generation == session[0]:
                recorder.counters['queries_observed'] += 1
                recorder.emit(session[0], event)
