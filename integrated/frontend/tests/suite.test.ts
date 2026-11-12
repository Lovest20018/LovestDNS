import assert from 'node:assert/strict';
import test from 'node:test';
import { appendTrend, trendSegments } from '../src/trend.ts';
import { configDiff } from '../src/configDiff.ts';
import { parseSuiteReport, REPORT_LIMIT } from '../src/reports.ts';
import type { RuntimeSnapshot } from '../src/suiteApi.ts';
import { runtimeLifetimeMs } from '../src/runtimeFreshness.ts';

function sample(at: number, comparable = true): RuntimeSnapshot {
  return { schema_version: 1, sampled_at: at, ready: true, core_healthy: true, components: {},
    layers: { recursive: { available: true, comparable, qps: 0, cache_hit_ratio: 0 } },
    redis: { available: true, info: { used_memory: 0 } } };
}

test('server freshness survives browser clock skew and still expires stale samples', () => {
  const value = { ...sample(100), freshness: { age_seconds: 3, max_age_seconds: 25 } };
  assert.equal(runtimeLifetimeMs(value, 250, 0), 21750);
  assert.equal(runtimeLifetimeMs(value, 250, 9999999999999), 21750);
  assert.throws(() => runtimeLifetimeMs({ ...value, freshness: { age_seconds: 26, max_age_seconds: 25 } }));
  assert.throws(() => runtimeLifetimeMs({ ...value, freshness: { age_seconds: -1, max_age_seconds: 25 } }));
  assert.throws(() => runtimeLifetimeMs({ ...value, freshness: { age_seconds: NaN, max_age_seconds: 25 } }));
  assert.throws(() => runtimeLifetimeMs(value, 22000));
  assert.equal(runtimeLifetimeMs({ ...value, freshness: { age_seconds: 3, max_age_seconds: 900 } }, 0), 22000);
  assert.equal(runtimeLifetimeMs(sample(100), 0, 105000), 20000);
});
test('trend preserves real zeroes, rejects counter reset samples, and breaks across missing data', () => {
  let points = appendTrend([], sample(10), 10);
  points = appendTrend(points, sample(15, false), 15);
  points = appendTrend(points, sample(20), 20);
  points = appendTrend(points, null, 25);
  assert.deepEqual(points.map((p) => p.recursive), [0, null, 0, null]);
  assert.equal(trendSegments(points, 'recursive').length, 2);
  assert.equal(points[0].memory, 0);
  assert.equal(appendTrend(points, sample(20), 30), points);
});
test('trend is bounded by count and age; long gaps split the graph and a reload starts empty', () => {
  let points = [] as ReturnType<typeof appendTrend>;
  for (let i = 1; i <= 450; i++) points = appendTrend(points, sample(i * 5), i * 5);
  assert.equal(points.length, 360);
  const newer = appendTrend(points, sample(5000), 5000);
  assert.equal(newer.length, 1);
  assert.equal(appendTrend([], sample(6000), 6000).length, 1);
  assert.equal(trendSegments([points[0], points.at(-1)!], 'recursive').length, 2);
});
test('config diff compares removed and restored lines and safely limits large input', () => {
  const diff = configDiff('A="1"\nB="2"', 'A="1"\nB="3"')!;
  assert.deepEqual(diff.map((line) => line.kind), ['same', 'remove', 'add']);
  assert.equal(diff.filter((line) => line.kind !== 'add').map((line) => line.text).join('\n'), 'A="1"\nB="2"');
  assert.equal(configDiff('line\n'.repeat(700), ''), null);
  assert.deepEqual(configDiff('same', 'same'), [{ kind: 'same', text: 'same' }]);
});
function report(record = 'TXT', available = true) {
  return { kind: 'paopaodns-dns-report', schema_version: 1, started_utc: '2026-10-25T00:00:00Z', method: 'Test',
    tests: [{ label: '核心', result: { domain: 'example.com', record_type: record, server: '127.0.0.1', port: 53,
      available, status: available ? 'ok' : 'no_records', rcode: 'NOERROR', transport: 'tcp', tcp_fallback: true,
      results: ['text'], answers: [{ name: 'example.com', type: record, ttl: 0, class: 1, value: 'text' }],
      debug: [{ name: '1.addinfo.paopaodns', value: '<script>literal</script>', ttl: 0 }] } }] };
}
test('report import supports all suite records, TTL zero, TCP fallback and literal debug text', () => {
  for (const type of ['A', 'AAAA', 'CNAME', 'TXT', 'HTTPS', 'MX', 'NS', 'SRV', 'PTR', 'SVCB']) {
    const parsed = parseSuiteReport(JSON.stringify(report(type)));
    assert.equal(parsed.tests[0].result.answers[0].ttl, 0);
    assert.equal(parsed.tests[0].result.record_type, type);
    assert.equal(parsed.tests[0].result.tcp_fallback, true);
    assert.equal(parsed.tests[0].result.debug?.[0].value, '<script>literal</script>');
  }
});
test('report rejects oversized, unsupported, truncated and contradictory successes', () => {
  assert.throws(() => parseSuiteReport(' '.repeat(REPORT_LIMIT + 1)));
  assert.throws(() => parseSuiteReport(JSON.stringify(report('ANY'))));
  const value = report();
  value.tests[0].result.answers[0].name = 'other.example';
  assert.throws(() => parseSuiteReport(JSON.stringify(value)));
  const truncated = { ...report().tests[0].result, truncated: true };
  assert.throws(() => parseSuiteReport(JSON.stringify({ ...report(), tests: [{ label: 'bad', result: truncated }] })));
  const empty = report(); empty.tests = [];
  assert.throws(() => parseSuiteReport(JSON.stringify(empty)));
});
