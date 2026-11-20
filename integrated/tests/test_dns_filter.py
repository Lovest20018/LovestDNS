import json
from io import BytesIO
import time

import pytest

from runtime import dns_filter, filter_download
from runtime.filter_download import destination, public_address
from runtime.filter_rules import compile_policy, parse
from runtime.query_recorder import Recorder
from test_integrated_api import client


def test_supported_rule_boundaries_and_unsupported_semantics():
    text = ('\ufeff! comment ' + 'x' * 550 + '\r\nADS.example.test. # local\r\nads.example.test\r\n'
            '||track.example.test^\r\n@@||allow.track.example.test^\r\n'
            '0.0.0.0 example.test alias.example.test # https://example.test/source##marker $badfilter\r\n:: v6.example.test\r\n'
            '192.0.2.2 custom.example.test\r\nexample.test##.advert\r\n/ads[0-9]/\r\n'
            '||ads.example.test^$dnstype=A\r\n@@||ads.example.test^$client=192.0.2.2\r\n'
            '||ads.example.test^$dnstype=A,badfilter\r\n!#include https://example.test')
    terms, report = parse(text)
    assert [(kind, name) for kind, name, _, _ in terms] == [
        ('exact', 'ads.example.test'), ('suffix', 'track.example.test'), ('except', 'allow.track.example.test'),
        ('exact', 'example.test'), ('exact', 'alias.example.test'), ('exact', 'v6.example.test'),
    ]
    assert [report[k] for k in ('valid', 'duplicates', 'ignored', 'unsupported', 'invalid', 'protected')] == [6, 1, 1, 4, 0, 3]
    assert report['types'] == ['cosmetic', 'hosts_rewrite', 'modifier:dnstype', 'pattern']
    assert report['issues'][4]['rule'] == '@@||ads.example.test^$client=192.0.2.2'


@pytest.mark.parametrize('value', ['http://example.test', 'https://u:p@example.test/', 'https://example.test:8080/',
                                  'https://example.test/#x', 'https://example.test\\invalid', ''])
def test_subscription_urls_must_be_public_https(value):
    with pytest.raises(ValueError):
        destination(value)


def test_supported_ip_classes_for_subscription_connection():
    assert all(not public_address(ip) for ip in ('127.0.0.1', '10.9.8.7', '100.64.0.1', '169.254.169.254',
                                                '224.0.0.1', '::1', '::ffff:127.0.0.1', '64:ff9b:1::1', 'fc00::1'))
    assert public_address('1.1.1.1') and public_address('2606:4700:4700::1111')


def test_https_subscription_get_checks_redirect_and_reads_text(monkeypatch):
    import http.client
    requests = []
    body = b'||ad.example.test^\r\n'
    replies = iter((b'HTTP/1.1 302 Found\r\nLocation: https://lists.example.test/list\r\nContent-Length: 0\r\n\r\n',
                    b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\n\r\n' + body))

    class Peer:
        def settimeout(self, _):
            pass

        def close(self):
            pass

        def makefile(self, _):
            return BytesIO(next(replies))

    class Connection:
        def __init__(self, host, ips, deadline):
            self.sock = Peer()
            requests.append((host, ips))

        def request(self, method, path, *, headers):
            assert method == 'GET' and path in ('/', '/list') and headers['Accept-Encoding'] == 'identity'

        def getresponse(self):
            response = http.client.HTTPResponse(self.sock)
            response.begin()
            return response

        def close(self):
            pass

    def resolve(host, deadline):
        return ['1.1.1.1'] if host == 'origin.example.test' else ['1.0.0.1']

    monkeypatch.setattr(filter_download, 'resolve', resolve)
    monkeypatch.setattr(filter_download, 'PublicHTTPS', Connection)
    assert filter_download.download('https://origin.example.test/') == body.decode()
    assert requests == [('origin.example.test', ['1.1.1.1']), ('lists.example.test', ['1.0.0.1'])]


@pytest.fixture
def service(tmp_path, monkeypatch):
    loaded = {'revision': '', 'enabled': False}
    calls, feed = [], ['||ad.example.test^']

    def activate(_, revision, verify=True):
        calls.append((revision, verify))
        loaded['revision'] = revision
        loaded['enabled'] = bool(revision and manager.store.model(revision)['enabled'])
        if verify and state['fail']:
            raise ValueError('synthetic activation verification failure')

    state = {'fail': False}
    monkeypatch.setattr(dns_filter, 'activate_core', activate)
    monkeypatch.setattr(dns_filter, 'core_call', lambda *args, **kwargs: loaded.copy())
    manager = dns_filter.FilterManager(tmp_path, tmp_path, downloader=lambda _: feed[0])
    return manager, loaded, feed, calls, state


def run(manager, action, **kwargs):
    submitted = manager.submit({'action': action, 'revision': manager.store.revision(), **kwargs})
    manager.step()
    state = manager.status()
    return state, next(job for job in state['jobs'] if job['id'] == submitted['id'])


def test_failed_candidate_restores_durable_policy_and_restart_reconciles(service):
    manager, loaded, _, calls, state = service
    value, job = run(manager, 'configure', enabled=True, local_rules='||ad.example.test^')
    assert job['phase'] == 'succeeded' and value['revision'] == loaded['revision']
    old = value['revision']
    state['fail'] = True
    value, job = run(manager, 'configure', enabled=False, local_rules='')
    assert job['phase'] == 'failed' and value['revision'] == loaded['revision'] == old and value['live_enabled']
    assert calls[-1] == (old, False)
    loaded['revision'] = '9' * 64
    manager.step()
    assert loaded['revision'] == old
    with pytest.raises(ValueError, match='配置已变化'):
        manager.submit({'action': 'configure', 'revision': '', 'enabled': False, 'local_rules': ''})


def test_subscription_preview_partial_refresh_and_protected_failure(service):
    manager, _, feed, _, _ = service
    feed[0] += '\n||else.example.test^$dnstype=A'
    state, job = run(manager, 'preview', url='https://example.test/filter?k=private-fixture', name='test', interval=0)
    assert job['phase'] == 'succeeded' and 'private-fixture' not in json.dumps(state)
    preview = state['draft']['id']
    _, job = run(manager, 'apply_preview', preview=preview, partial=False)
    assert job['phase'] == 'failed'
    state, job = run(manager, 'apply_preview', preview=preview, partial=True)
    assert job['phase'] == 'succeeded' and 'private-fixture' not in json.dumps(state)
    old, source = state['revision'], state['sources'][0]['id']
    for extra in ('||new.example.test^$important', '@@||ad.example.test^$client=192.0.2.30'):
        feed[0] = '||ad.example.test^\n' + extra
        updated, job = run(manager, 'refresh', id=source)
        assert job['phase'] == 'failed' and updated['draft'] and updated['live_revision'] == old
    _, job = run(manager, 'apply_preview', preview=updated['draft']['id'], partial=True)
    assert job['phase'] == 'failed'
    feed[0] = '||replacement.example.test^\n||else.example.test^$dnstype=AAAA'
    state, job = run(manager, 'refresh', id=source)
    assert job['phase'] == 'succeeded' and state['live_revision'] != old


def test_unbound_mode_reports_unavailable_and_retains_configuration(service):
    manager, _, _, _, _ = service
    state, job = run(manager, 'configure', enabled=True, local_rules='ad.example.test')
    assert job['phase'] == 'succeeded'
    (manager.store.run / 'core-env.json').write_text('{"CNAUTO":"no"}')
    current, job = run(manager, 'allow', domain='ad.example.test', mode='permanent')
    assert job['phase'] == 'failed' and current['revision'] == state['revision']
    assert current['mode'] == 'recursive' and current['enabled'] and not current['live_enabled']


def test_clear_and_disable_discard_delayed_core_events(tmp_path):
    recorder = Recorder(tmp_path, tmp_path)
    recorder.configure(True)
    generation = recorder.generation
    token, epoch = 'a' * 32, 'b' * 32
    recorder.core_events.session = (generation, 9000, token, epoch)
    now = time.time_ns() // 1000
    value = {'token': token, 'epoch': epoch, 'sequence': 1, 'client': '127.0.0.1:12121',
             'entry': '127.0.0.1:5533', 'name': 'ad.example.test.', 'started_us': now, 'finished_us': now,
             'qtype': 1, 'qclass': 1, 'elapsed_ms': 0, 'rcode': 5, 'transport': 'udp', 'answer_count': 0,
             'addresses': [], 'addresses_complete': True, 'truncated': False,
             'filter': {'action': 'block', 'revision': 'c' * 64, 'id': 'a' * 24, 'source': 'fixture', 'rule': 'ad.example.test'}}
    recorder.core_events.ingest(value)
    assert recorder.events.get_nowait()[1]['filter']['action'] == 'block'
    recorder.request({'operation': 'clear'})
    recorder.core_events.session = (recorder.generation, 9000, token, 'd' * 32)
    recorder.core_events.ingest(value)
    assert recorder.events.empty()
    recorder.configure(False)
    recorder.core_events.ingest({**value, 'epoch': 'd' * 32})
    assert recorder.events.empty()


def test_filter_and_analytics_routes_are_private(client, monkeypatch):
    api = client[0]
    monkeypatch.setenv('SUITE_READ_TOKEN', 'observer-test')
    forbidden = {'Authorization': 'Bearer observer-test'}
    for route in ('/dns-filter', '/dns-filter/check?domain=ad.example.test', '/query-records/summary'):
        assert api.get('/api/v1' + route).status_code == 401
        assert api.get('/api/v1' + route, headers=forbidden).status_code == 403
    assert api.post('/api/v1/dns-filter/actions', headers=forbidden, json={}).status_code == 403
def test_literal_fragment_requires_subscription_review_on_upgrade():
    import json
    text = '*-ad.example.test*\n*-AD.example.test*\n*foo*bar.example.test*\n@@*unsafe.example.test*'
    terms, report = parse(text)
    assert [term[:2] for term in terms] == [('fragment', '-ad.example.test')]
    assert (report['duplicates'], report['unsupported'], report['protected']) == (1, 1, 1)
    source = {'id': 'test', 'name': 'test', 'content': 'test', 'enabled': True, 'accepted': ['pattern']}
    model = {'enabled': True, 'local_rules': '', 'sources': [source], 'allow': {}}
    old, count = compile_policy(model, lambda _: '*-ad.example.test*', 0)
    assert json.loads(old)['version'] == 1 and count == 0
    source['accepted'].append('fragment')
    new, count = compile_policy(model, lambda _: '*-ad.example.test*', 0)
    assert count == 1 and json.loads(new)['version'] == 2
    assert json.loads(new)['fragments'][0]['rule'] == '*-ad.example.test*'
    with pytest.raises(ValueError, match='128'):
        parse('\n'.join(f'*-{i}.example*' for i in range(129)))
