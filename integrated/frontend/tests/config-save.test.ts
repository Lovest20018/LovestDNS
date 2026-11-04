import assert from 'node:assert/strict';
import { test } from 'node:test';
import { preflightSave } from '../src/configSave.ts';

test('concurrent change rejects save without touching disk or losing caller draft', async () => {
  let disk = 'changed elsewhere'; let writes = 0;
  const draft = '# local draft';
  await assert.rejects(preflightSave('custom_env.ini', 'original', draft,
    async () => ({ content: disk }), async (_, content) => { disk = content; writes++; }), /已阻止覆盖/);
  assert.equal(writes, 0); assert.equal(disk, 'changed elsewhere'); assert.equal(draft, '# local draft');
});

test('missing file is distinct from empty file and supports first creation', async () => {
  let written: string | null = null;
  const missing = async () => { throw Object.assign(new Error('missing'), { status: 404 }); };
  await preflightSave('new.txt', null, 'first', missing, async (_, content) => { written = content; });
  assert.equal(written, 'first');
  await assert.rejects(preflightSave('new.txt', null, 'draft', async () => ({ content: '' }), async () => {}), /已阻止覆盖/);
  await assert.rejects(preflightSave('new.txt', '', 'draft', missing, async () => {}), /已阻止覆盖/);
});

test('read failures never write; retry after recovery uses same draft and baseline', async () => {
  let failed = true; let disk = 'original'; let writes = 0;
  const read = async () => { if (failed) throw new Error('temporarily unavailable'); return { content: disk }; };
  const write = async (_: string, content: string) => { disk = content; writes++; return { saved: true }; };
  await assert.rejects(preflightSave('file', 'original', 'draft', read, write), /temporarily unavailable/);
  assert.equal(writes, 0);
  failed = false;
  assert.deepEqual(await preflightSave('file', 'original', 'draft', read, write), { saved: true });
  assert.equal(disk, 'draft'); assert.equal(writes, 1);
});


test('write uses the exact read revision so a change after preflight is rejected by server', async () => {
  let expected: string | undefined;
  await assert.rejects(preflightSave('file', 'original', 'draft',
    async () => ({ content: 'original', revision: 'read-version' }),
    async (_, __, token) => { expected = token; throw Object.assign(new Error('文件已改变'), { status: 412 }); }), /文件已改变/);
  assert.equal(expected, 'read-version');
});
