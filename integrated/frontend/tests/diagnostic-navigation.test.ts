import assert from 'node:assert/strict';
import test from 'node:test';
import { diagnosticHash, parseDiagnosticRoute, queryRecordParams } from '../src/diagnosticRoute.ts';

test('diagnostic entry opens records by default, queries from existing domain links, and respects read-only access', () => {
  assert.equal(parseDiagnosticRoute('#/diagnostics').tab, 'records');
  assert.equal(parseDiagnosticRoute('#/diagnostics?domain=example.com').tab, 'query');
  assert.equal(parseDiagnosticRoute('#/diagnostics?tab=query').tab, 'query');
  assert.equal(parseDiagnosticRoute('#/diagnostics?tab=compare').tab, 'compare');
  assert.equal(parseDiagnosticRoute('#/diagnostics?tab=records&domain=example.com').tab, 'records');
  assert.equal(parseDiagnosticRoute('#/diagnostics?tab=records', '', false).tab, 'query');
});

test('shareable links preserve independent record filters, active query, pagination and refresh choice', () => {
  const route = { ...parseDiagnosticRoute('#/diagnostics'), source: '2001:db8::20',
    filterDomain: 'one&two.example', qtype: '28', before: 73, automatic: true,
    domain: 'active.example.', record: 'AAAA' };
  const hash = diagnosticHash(route);
  assert.deepEqual(parseDiagnosticRoute(hash), route);
  assert.deepEqual(Object.fromEntries(queryRecordParams(parseDiagnosticRoute(hash))), {
    limit: '50', source: '2001:db8::20', domain: 'one&two.example', qtype: '28', before: '73',
  });
  const query = parseDiagnosticRoute(diagnosticHash({ ...route, tab: 'query', domain: 'another.example.' }));
  assert.equal(query.filterDomain, 'one&two.example');
  assert.equal(query.domain, 'another.example.');
  assert.equal(query.record, 'AAAA');
});

test('return navigation restores context while an explicit link takes precedence over remembered filters', () => {
  const remembered = '#/diagnostics?tab=records&source=192.0.2.20&before=45&auto=1';
  assert.equal(parseDiagnosticRoute('#/diagnostics', remembered).source, '192.0.2.20');
  assert.equal(parseDiagnosticRoute('#/diagnostics', remembered).before, 45);
  const shared = parseDiagnosticRoute('#/diagnostics?tab=records&source=192.0.2.21', remembered);
  assert.equal(shared.source, '192.0.2.21');
  assert.equal(shared.before, null);
  assert.equal(shared.automatic, false);
});

test('malformed or unsafe cursors cannot turn into API pagination parameters', () => {
  for (const cursor of ['0', '-1', '1.5', 'Infinity', '9007199254740992', '1e3', 'oops']) {
    const route = parseDiagnosticRoute(`#/diagnostics?before=${cursor}&qtype=70000`);
    assert.equal(route.before, null);
    assert.equal(queryRecordParams(route).has('before'), false);
    assert.equal(route.qtype, '');
  }
});

test('home and domain-list query shortcuts keep record filters without contaminating explicit record links', () => {
  const remembered = '#/diagnostics?tab=records&source=192.0.2.20&filter_domain=sample&qtype=28&before=45&auto=1';
  for (const link of ['#/diagnostics?tab=query', '#/diagnostics?tab=compare', '#/diagnostics?domain=example.com']) {
    const route = parseDiagnosticRoute(link, remembered);
    assert.equal(route.source, '192.0.2.20');
    assert.equal(route.filterDomain, 'sample');
    assert.equal(route.qtype, '28');
    assert.equal(route.before, 45);
    assert.equal(route.automatic, true);
  }
  const explicit = parseDiagnosticRoute('#/diagnostics?tab=records', remembered);
  assert.equal(explicit.source, '');
  assert.equal(explicit.before, null);
});
