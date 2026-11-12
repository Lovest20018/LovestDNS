import assert from 'node:assert/strict';
import test from 'node:test';
import { configurationFingerprint, processFingerprint } from '../src/queryEvidence.ts';
import type { RuntimeSnapshot } from '../src/suiteApi.ts';

test('counter changes preserve identity but restarts invalidate sample evidence', () => {
  const runtime = { ready: true, components: Object.fromEntries(['mosdns', 'unbound-raw', 'unbound-forward', 'dnscrypt', 'dnscrypt-socks', 'redis'].map((name, i) => [name, { statename: 'RUNNING', pid: 22 + i, start: 100, exitstatus: 0 }])) } as unknown as RuntimeSnapshot;
  assert.notEqual(processFingerprint(runtime), null);
  assert.equal(processFingerprint(runtime), processFingerprint({ ...runtime, sampled_at: 200 }));
  assert.notEqual(processFingerprint(runtime), processFingerprint({ ...runtime, components: { ...runtime.components, mosdns: { ...runtime.components.mosdns, start: 101 } } }));
  assert.equal(processFingerprint({ ...runtime, ready: false }), null);
  assert.equal(processFingerprint({ ...runtime, components: {} }), null);
  assert.equal(processFingerprint({ ...runtime, components: { ...runtime.components, mosdns: { ...runtime.components.mosdns, pid: 0 } } }), null);
});
test('configuration identity is independent of row order, not revision changes', () => {
  const files = [{ filename: 'a', revision: 'first' }, { filename: 'b', revision: 'second' }];
  assert.equal(configurationFingerprint({ files }), configurationFingerprint({ files: [...files].reverse() }));
  assert.notEqual(configurationFingerprint({ files }), configurationFingerprint({ files: [{ ...files[0], revision: 'third' }, files[1]] }));
});
