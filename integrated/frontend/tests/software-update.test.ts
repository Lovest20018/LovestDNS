import assert from 'node:assert/strict';
import test from 'node:test';
import { setBrowserCsrf, softwareUpdate, softwareUpdateNetwork } from '../src/suiteApi.ts';

const status = { schema_version: 1, supported: true, current_version: 'test.old', phase: 'ready',
  release: { version: 'test.next', archive_sha256: 'a'.repeat(64), archive_bytes: 2000, prerelease: true,
    published_at: '2026-01-01T00:00:00Z', notes: 'Fixture notes',
    url: 'https://github.com/Lovest20018/LovestDNS/releases/tag/vtest.next' } };

test('update submission uses browser CSRF and pins exactly the checked version and digest', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; setBrowserCsrf(''); });
  setBrowserCsrf('fixture-csrf');
  const requests: RequestInit[] = [];
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/auth/software-update'); requests.push(init!);
    return new Response(JSON.stringify(status));
  };
  await softwareUpdate({ action: 'check' });
  const body = { action: 'apply' as const, version: 'test.next', archive_sha256: 'a'.repeat(64) };
  await softwareUpdate(body);
  assert.equal(requests[1].method, 'POST');
  assert.equal(requests[1].credentials, 'same-origin');
  assert.equal(new Headers(requests[1].headers).get('X-CSRF-Token'), 'fixture-csrf');
  assert.deepEqual(JSON.parse(requests[1].body as string), body);
  assert.equal(new Headers(requests[0].headers).get('X-CSRF-Token'), 'fixture-csrf');
});

test('malformed update states and foreign release links cannot reach the settings UI', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  for (const value of [null, { ...status, phase: 'unknown' }, { ...status, message: {} },
    { ...status, release: { ...status.release, archive_sha256: 'unverified' } },
    { ...status, release: { ...status.release, url: 'javascript:alert(1)' } }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(value));
    await assert.rejects(softwareUpdate(), /格式无效/);
  }
});

test('unsupported deployments remain visible and request failures do not become successful updates', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async () => new Response(JSON.stringify({ schema_version: 1, supported: false, current_version: 'test.old', reason: 'Native only' }));
  assert.equal((await softwareUpdate()).supported, false);
  globalThis.fetch = async () => new Response(JSON.stringify({ error: 'Already running' }), { status: 409 });
  await assert.rejects(softwareUpdate({ action: 'check' }), /Already running/);
  globalThis.fetch = async () => { throw new TypeError('Connection reset'); };
  await assert.rejects(softwareUpdate(), /Connection reset/);
});

test('network settings use authenticated CSRF writes and reject secret-bearing responses', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; setBrowserCsrf(''); });
  const value = { mode: 'socks5' as const, host: '192.0.2.10', port: 1080, username: 'fixture', password_configured: true };
  setBrowserCsrf('fixture-csrf');
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/auth/software-update/network');
    assert.equal(init?.method, 'PUT');
    assert.equal(new Headers(init?.headers).get('X-CSRF-Token'), 'fixture-csrf');
    assert.equal(init?.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(init?.body as string), { mode: 'socks5', host: value.host, port: 1080, username: 'fixture', password: 'synthetic-secret' });
    return new Response(JSON.stringify(value));
  };
  assert.deepEqual(await softwareUpdateNetwork({ mode: 'socks5', host: value.host, port: 1080, username: 'fixture', password: 'synthetic-secret' }), value);
  for (const bad of [{ ...value, password: 'must-not-return' }, { ...value, mode: 'system' }, { ...value, port: '1080' }, { ...value, password_configured: null }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(bad));
    await assert.rejects(softwareUpdateNetwork(), /格式无效/);
  }
});
