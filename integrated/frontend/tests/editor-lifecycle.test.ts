import assert from 'node:assert/strict';
import test from 'node:test';
import { parseListContent, serializeListContent } from '../src/domainEditor.ts';
import { parseTtlRules, serializeTtlRules } from '../src/ttlEditor.ts';
import { applyFileSave, clearFileDrafts, draftEpoch, dirtyFileDrafts, fileBaselineVersion, fileDraft, invalidateCleanDraft, reconcileFileDraft, rememberFileDraft, setDraftInstance, subscribeFileDraft } from '../src/fileDrafts.ts';
import { parseCustomEnvDocument, serializeCustomEnvDocument, validateCustomEnvRows } from '../src/customEnvEditor.ts';

test('domain visual round trip preserves BOM, blanks, comments and untouched rules', () => {
  const source = '\uFEFF# comment\r\n\r\n  domain:example.com  \r\n#\r\nunknown:keep\r\n';
  const rows = parseListContent(source);
  assert.equal(serializeListContent(rows), source);
  const updated = rows.map((row) => row.prefix === 'domain' && row.value.includes('example.com') ? { ...row, value: 'changed.example' } : row);
  assert.equal(serializeListContent(updated), source.replace('  domain:example.com  \r', 'domain:changed.example'));
});

test('TTL editing retains custom comments and exact source without adding boilerplate', () => {
  const source = '# private explanation\r\n\r\n example.com@@192.0.2.1  \r\n# trailing\r\n';
  const parsed = parseTtlRules(source);
  assert.equal(parsed.visualSafe, true);
  assert.equal(serializeTtlRules(parsed.entries, source), source);
  assert.equal(serializeTtlRules(parsed.entries.map((row) => ({ ...row, record: '192.0.2.2' })), source), source.replace(' example.com@@192.0.2.1  \r', 'example.com@@192.0.2.2'));
  assert.equal(parseTtlRules('example.com@@@@bad').visualSafe, false);
  assert.equal(parseTtlRules('unknown syntax').visualSafe, false);
});

test('shared drafts survive navigation/auth recovery and reject late updates after logout or instance change', () => {
  clearFileDrafts(); setDraftInstance('instance-a');
  const value = { content: 'draft', baseline: 'original', revision: 'old', loaded: true };
  const epoch = draftEpoch();
  rememberFileDraft('custom_env.ini', value, epoch);
  assert.equal(fileDraft('custom_env.ini'), value);
  assert.equal(dirtyFileDrafts(), true);
  setDraftInstance('instance-a');
  assert.equal(fileDraft('custom_env.ini'), value);
  setDraftInstance('instance-b');
  assert.equal(fileDraft('custom_env.ini'), undefined);
  assert.equal(rememberFileDraft('custom_env.ini', value, epoch), false);
  const next = draftEpoch(); clearFileDrafts();
  assert.equal(rememberFileDraft('custom_env.ini', value, next), false);
  assert.equal(dirtyFileDrafts(), false);
});

test('new or temporarily incomplete visual rows remain editable until validation', () => {
  const env = serializeCustomEnvDocument('# original\n', [{ key: '', value: '', enabled: true, sourceLine: null }]);
  const rows = parseCustomEnvDocument(env).entries;
  assert.equal(rows.length, 1);
  assert.throws(() => validateCustomEnvRows(rows));
  const completed = serializeCustomEnvDocument(env, [{ ...rows[0], key: 'VALUE', value: 'ready' }]);
  assert.equal(parseCustomEnvDocument(completed).entries[0].key, 'VALUE');
  assert.equal(parseListContent('domain:')[0].value, '');
  const ttl = serializeTtlRules([{ id: 99, domain: '', server: '', type: 'forward' }], '');
  assert.equal(parseTtlRules(ttl).entries.length, 1);
});

test('remote revalidation updates clean documents and preserves conflicted drafts', () => {
  const clean = { content: 'old', baseline: 'old', revision: 'a', loaded: true };
  const remote = { content: 'external', baseline: 'external', revision: 'b', loaded: true };
  assert.equal(reconcileFileDraft(clean, remote).document, remote);
  const dirty = { ...clean, content: 'draft' };
  assert.deepEqual(reconcileFileDraft(dirty, remote), { document: dirty, conflict: true });
  assert.equal(reconcileFileDraft(dirty, remote, true).document, remote);
  clearFileDrafts(); rememberFileDraft('file', dirty); invalidateCleanDraft('file');
  assert.equal(fileDraft('file'), dirty);
  rememberFileDraft('file', clean); invalidateCleanDraft('file');
  assert.equal(fileDraft('file'), undefined);
});

test('shared subscribers receive later save baselines without losing newer edits', () => {
  clearFileDrafts();
  let updates = 0;
  const stop = subscribeFileDraft('file', () => updates++);
  rememberFileDraft('file', { content: 'saving', baseline: 'old', revision: 'a', loaded: true });
  const version = fileBaselineVersion('file');
  rememberFileDraft('file', { ...fileDraft('file')!, content: 'newer edit' });
  assert.equal(applyFileSave('file', version, 'saving', 'b'), true);
  assert.equal(fileDraft('file')!.content, 'newer edit');
  assert.equal(fileDraft('file')!.revision, 'b');
  assert.equal(updates, 3);
  stop(); clearFileDrafts(); assert.equal(updates, 3);
});

test('delayed save cannot roll back a newer shared read and completed save', () => {
  clearFileDrafts();
  rememberFileDraft('file', { content: 'first', baseline: 'old', revision: 'old-revision', loaded: true });
  const first = fileBaselineVersion('file');
  // Another route explicitly re-reads the first committed write, then saves a second edit.
  rememberFileDraft('file', { content: 'first', baseline: 'first', revision: 'first-revision', loaded: true });
  rememberFileDraft('file', { ...fileDraft('file')!, content: 'second' });
  const second = fileBaselineVersion('file');
  assert.equal(applyFileSave('file', second, 'second', 'second-revision'), true);
  const latest = fileDraft('file');
  assert.equal(applyFileSave('file', first, 'first', 'first-revision'), false);
  assert.equal(fileDraft('file'), latest);
  assert.equal(dirtyFileDrafts(), false);
  assert.equal(fileDraft('file')!.revision, 'second-revision');
  clearFileDrafts();
});

test('baseline generations reject stale saves after returning to an older revision', () => {
  clearFileDrafts();
  const original = { content: 'old', baseline: 'old', revision: 'a', loaded: true };
  rememberFileDraft('file', original);
  const version = fileBaselineVersion('file');
  rememberFileDraft('file', { content: 'external', baseline: 'external', revision: 'b', loaded: true });
  rememberFileDraft('file', original);
  assert.equal(applyFileSave('file', version, 'stale', 'c'), false);
  assert.equal(fileDraft('file'), original);
  clearFileDrafts();
});

test('saved responses cannot recreate invalidated drafts or cross instance epochs', () => {
  clearFileDrafts(); setDraftInstance('first-instance');
  const clean = { content: 'old', baseline: 'old', revision: 'a', loaded: true };
  rememberFileDraft('file', clean);
  const version = fileBaselineVersion('file'), epoch = draftEpoch();
  invalidateCleanDraft('file');
  assert.equal(applyFileSave('file', version, 'stale', 'b', epoch), false);
  rememberFileDraft('file', clean);
  setDraftInstance('second-instance');
  rememberFileDraft('file', clean);
  assert.equal(applyFileSave('file', fileBaselineVersion('file'), 'stale', 'b', epoch), false);
  assert.equal(fileDraft('file'), clean);
  clearFileDrafts();
});

test('global draft subscriber tracks edits across files and logout for maintenance guards', () => {
  clearFileDrafts();
  const observed: boolean[] = [];
  const stop = subscribeFileDraft('*', () => observed.push(dirtyFileDrafts()));
  rememberFileDraft('first', { content: 'edit', baseline: '', revision: 'a', loaded: true });
  rememberFileDraft('second', { content: 'same', baseline: 'same', revision: 'b', loaded: true });
  rememberFileDraft('first', { content: '', baseline: '', revision: 'a', loaded: true });
  clearFileDrafts(); stop();
  assert.deepEqual(observed, [true, true, false, false]);
});
