import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, setBrowserCsrf, suiteFetch } from '../src/suiteApi.ts';

const envelope = (data: unknown) => new Response(JSON.stringify({ data, meta: { api_version: '1', request_id: 'test-id' } }), { headers: { 'Content-Type': 'application/json' } });

test('request deadline remains active with an external abort signal', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  globalThis.fetch = async (_url, init) => new Promise((_resolve, reject) => {
    const signal = init!.signal!;
    if (signal.aborted) reject(signal.reason);
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  });
  const controller = new AbortController();
  // Keep the test loop alive while AbortSignal's unref'ed timer is pending.
  const keepAlive = setTimeout(() => {}, 1000);
  try {
    await assert.rejects(suiteFetch('/api/status', { signal: controller.signal, timeoutMs: 10 }), (error: ApiError) => error.code === 'TIMEOUT' && error.retryable);
    await assert.rejects(suiteFetch('/api/file/custom_env.ini', { signal: controller.signal, method: 'PUT', timeoutMs: 10 }), (error: ApiError) => error.code === 'RESULT_UNCONFIRMED' && !error.retryable);
  }
  finally { clearTimeout(keepAlive); }
  controller.abort(new DOMException('Unmounted', 'AbortError'));
  await assert.rejects(suiteFetch('/api/status', { signal: controller.signal }), { name: 'AbortError' });
});

test('parallel authentication failures trigger only one recovery check', async (t) => {
  const original = globalThis.fetch;
  const oldWindow = globalThis.window;
  const events = new EventTarget(); let expired = 0;
  events.addEventListener('lovestdns-auth-expired', () => expired++);
  Object.defineProperty(globalThis, 'window', { configurable: true, writable: true, value: events });
  t.after(() => { globalThis.fetch = original; Object.defineProperty(globalThis, 'window', { configurable: true, writable: true, value: oldWindow }); });
  setBrowserCsrf('fresh-session');
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: 'UNAUTHORIZED', message: 'expired' } }), { status: 401 });
  await Promise.allSettled([suiteFetch('/api/status'), suiteFetch('/api/runtime'), suiteFetch('/api/capabilities')]);
  assert.equal(expired, 1);
  setBrowserCsrf('next-session');
  await assert.rejects(suiteFetch('/api/status'), (error: ApiError) => error.code === 'UNAUTHORIZED' && error.status === 401);
  assert.equal(expired, 2);
});

test('invalid successful envelopes and malformed resource data become isolated API errors', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  for (const body of [{ unexpected: true }, { data: null }, { data: {}, meta: { api_version: '2' } }]) {
    globalThis.fetch = async () => new Response(JSON.stringify(body));
    await assert.rejects(suiteFetch('/api/status'), (error: ApiError) => error.code === 'INVALID_RESPONSE');
  }
  globalThis.fetch = async () => envelope({ schema_version: 99 });
  await assert.rejects(suiteFetch('/api/runtime'), (error: ApiError) => error.code === 'INVALID_RESPONSE');
  globalThis.fetch = async () => new Response('<html>proxy error</html>');
  await assert.rejects(suiteFetch('/api/status'), ApiError);
  globalThis.fetch = async () => new Response(JSON.stringify({ error: { code: 'SAVE_UNCONFIRMED', message: 'check file', details: { revision: 'new' } }, meta: { request_id: 'save-1234' } }), { status: 503 });
  await assert.rejects(suiteFetch('/api/file/custom_env.ini'), (error: ApiError) => error.code === 'SAVE_UNCONFIRMED' && error.requestId === 'save-1234' && !error.retryable);
});

test('malformed optional listener data and configuration revisions are rejected', async (t) => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const runtime = { schema_version: 1, sampled_at: 100, ready: true, core_healthy: true, components: {}, layers: {} };
  globalThis.fetch = async () => envelope(runtime);
  await suiteFetch('/api/runtime'); // Older collectors remain compatible.
  globalThis.fetch = async () => envelope({ ...runtime, dns: { listeners: [] } });
  await assert.rejects(suiteFetch('/api/runtime'), (error: ApiError) => error.code === 'INVALID_RESPONSE');
  globalThis.fetch = async () => envelope({ files: [{ filename: 'custom_env.ini', revision: null, state: 'recorded' }] });
  await assert.rejects(suiteFetch('/api/config/state'), (error: ApiError) => error.code === 'INVALID_RESPONSE');
});
