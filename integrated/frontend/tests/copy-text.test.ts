import assert from 'node:assert/strict';
import test from 'node:test';
import { copyText } from '../src/copyText.ts';

test('clipboard success copies the exact multiline command text', async () => {
  const content = '第一行\nprintf "literal $HOME"\n';
  let received = '';
  assert.equal(await copyText(content, { writeText: async text => { received = text; } }), true);
  assert.equal(received, content);
});

test('an unavailable clipboard on HTTP and denied clipboard permissions both request manual copy', async () => {
  assert.equal(await copyText('command', undefined), false);
  assert.equal(await copyText('command', {} as Clipboard), false);
  assert.equal(await copyText('command', { writeText: async () => { throw new Error('NotAllowedError'); } }), false);
  assert.equal(await copyText('command', { writeText: () => { throw new Error('Permission denied'); } }), false);
});
