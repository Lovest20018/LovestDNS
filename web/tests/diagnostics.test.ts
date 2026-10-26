import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeDiagnosticReport, parseDiagnosticFile } from '../src/diagnostics.ts';
import type { DnsTestResult } from '../src/api.ts';

const result: DnsTestResult = {
  available: true, domain: 'example.com', record_type: 'A', server: 'paopaodns', port: 53,
  answers: [{ name: 'example.com', type: 'A', value: '192.0.2.1', ttl: 0, class: 1 }],
  results: ['192.0.2.1'], elapsed_ms: 1.5, rcode: 'NOERROR', status: 'ok', transport: 'tcp',
  tcp_fallback: true,
};

function snapshot() {
  return {
    container: 'paopaodns', started_utc: '2026-10-23T09:00:00Z', elapsed_seconds: 10,
    method: 'Read-only snapshot; caches not cleared', stats_before: {}, stats_after: {},
    stats_delta: { 'total.num.queries': 10, 'total.requestlist.exceeded': 0 },
    observed_counters_reset: false, production_configs_unchanged: true,
    queries: [{ domain: 'example.com', role: 'frontend', port: 53, tcp: false,
      status: 'NOERROR', dns_ms: 3, exit: 0, stderr: '',
      records: [{ name: 'example.com.', type: 'A', value: '192.0.2.1', ttl: 0 }] }],
  };
}

test('Web UI report export/import retains results, TTL zero and TCP fallback', () => {
  const report = makeDiagnosticReport([{ label: '分流入口', result }]);
  const parsed = parseDiagnosticFile(JSON.stringify(report));
  assert.deepEqual(parsed.original, report);
  assert.equal(parsed.report.tests[0].result.status, 'ok');
  assert.equal(parsed.report.tests[0].result.answers[0].ttl, 0);
  assert.equal(parsed.report.tests[0].result.tcp_fallback, true);
  assert.equal(parsed.snapshot, null);
});

test('host snapshot import keeps original evidence and counter values', () => {
  const data = snapshot();
  const parsed = parseDiagnosticFile('\uFEFF' + JSON.stringify(data));
  assert.deepEqual(parsed.original, data);
  assert.equal(parsed.snapshot?.production_configs_unchanged, true);
  assert.equal(parsed.snapshot?.stats_delta['total.num.queries'], 10);
  assert.equal(parsed.report.tests[0].result.available, true);
});

test('negative deltas invalidate comparisons even when reset flag is false', () => {
  const data = snapshot();
  data.stats_delta['total.num.queries'] = -1;
  assert.equal(parseDiagnosticFile(JSON.stringify(data)).snapshot?.observed_counters_reset, true);
});

test('CNAME-only snapshot is not a successful A lookup', () => {
  const data = snapshot();
  data.queries[0].records = [{ name: 'example.com.', type: 'CNAME', value: 'target.example.com.', ttl: 60 }];
  const parsed = parseDiagnosticFile(JSON.stringify(data));
  assert.equal(parsed.report.tests[0].result.status, 'no_records');
});

test('a failed dig process never becomes successful from a partial answer', () => {
  const data = snapshot();
  data.queries[0].exit = 9;
  assert.equal(parseDiagnosticFile(JSON.stringify(data)).report.tests[0].result.available, false);
});

test('invalid, oversized and unsupported files are rejected', () => {
  for (const value of ['null', '{}', '[]', '{', JSON.stringify({ ...snapshot(), started_utc: 'invalid' }),
    JSON.stringify({ ...snapshot(), queries: Array(33).fill(snapshot().queries[0]) }),
    JSON.stringify({ ...snapshot(), stats_delta: { 'total.num.queries': 'invalid' } }),
    ' '.repeat(2 * 1024 * 1024 + 1)]) {
    assert.throws(() => parseDiagnosticFile(value));
  }
});

test('inconsistent imported success and truncated answers are rejected', () => {
  for (const invalid of [
    { ...result, status: 'no_records' }, { ...result, truncated: true },
    { ...result, answers: [] }, { ...result, rcode: 'SERVFAIL' },
    { ...result, available: false },
  ]) {
    const report = makeDiagnosticReport([{ label: 'test', result: invalid as DnsTestResult }]);
    assert.throws(() => parseDiagnosticFile(JSON.stringify(report)), /不一致/);
  }
});

test('a truncated host snapshot is not classified as resolved', () => {
  const data = { ...snapshot(), queries: [{ ...snapshot().queries[0], truncated: true }] };
  const parsed = parseDiagnosticFile(JSON.stringify(data));
  assert.equal(parsed.report.tests[0].result.available, false);
  assert.equal(parsed.report.tests[0].result.status, 'incomplete');
});

test('host snapshot with unreadable counters remains usable', () => {
  const data = { ...snapshot(), stats_before: { error: 'Permission denied' },
    stats_after: { error: 'Permission denied' }, stats_delta: {}, production_configs_unchanged: null };
  const parsed = parseDiagnosticFile(JSON.stringify(data));
  assert.equal(parsed.report.tests[0].result.available, true);
  assert.deepEqual(parsed.snapshot?.stats_delta, {});
  assert.equal(parsed.snapshot?.production_configs_unchanged, null);
});
