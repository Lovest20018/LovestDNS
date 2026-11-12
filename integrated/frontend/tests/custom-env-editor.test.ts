import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseCustomEnvDocument, serializeCustomEnvDocument, validateCustomEnvRows } from '../src/customEnvEditor.ts';
import { componentSummary, cacheSaveSummary } from '../src/runtimePresentation.ts';

test('visual env edits preserve arbitrary comments, duplicate rows, BOM and line endings', () => {
  const source = '\uFEFF# operator note\r\n\r\n# EXAMPLE="1"\r\nLOCAL_IP="192.0.2.1"\r\nLOCAL_IP="192.0.2.2"\r\n# end\r\n';
  const { entries } = parseCustomEnvDocument(source);
  assert.equal(entries.length, 3);
  assert.equal(serializeCustomEnvDocument(source, entries), source);
  entries[2].value = '192.0.2.9';
  assert.equal(serializeCustomEnvDocument(source, entries), source.replace('192.0.2.2', '192.0.2.9'));
  entries[0].enabled = true;
  assert.match(serializeCustomEnvDocument(source, entries), /\r\nEXAMPLE="1"\r\n/);
  entries.splice(1, 1);
  assert(!serializeCustomEnvDocument(source, entries).includes('192.0.2.1'));
  assert(serializeCustomEnvDocument(source, entries).includes('# operator note\r\n\r\n'));
});

test('comment-only template supports new rows and unrecognized syntax is retained', () => {
  const source = '# service settings\n# examples below';
  const rows = parseCustomEnvDocument(source).entries;
  rows.push({ key: 'LOCAL_IP', value: '192.0.2.3', enabled: true, sourceLine: null });
  const output = serializeCustomEnvDocument(source, rows);
  assert.equal(output, source + '\nLOCAL_IP="192.0.2.3"\n');
  validateCustomEnvRows(rows);
  assert.equal(serializeCustomEnvDocument(output, parseCustomEnvDocument(output).entries), output);
  const unknown = 'export A=1\n# note\nA="2"\n';
  const parsed = parseCustomEnvDocument(unknown);
  assert.equal(parsed.unrecognizedLines, 1);
  parsed.entries[0].enabled = false;
  assert.equal(serializeCustomEnvDocument(unknown, parsed.entries), 'export A=1\n# note\n# A="2"\n');
  assert.throws(() => validateCustomEnvRows([{ ...rows[0], key: '' }]));
  assert.throws(() => validateCustomEnvRows([{ ...rows[0], value: 'bad"quote' }]));
});

test('component labels distinguish completed work, disabled optional process and failed update', () => {
  const process = { statename: 'EXITED', pid: 0, start: 1, exitstatus: 0 };
  assert.equal(componentSummary('bootstrap', process).status, '准备完成');
  assert.equal(componentSummary('bootstrap', process).tone, 'good');
  assert.equal(componentSummary('updater', { ...process, exitstatus: 1 }).status, '本次更新未完成');
  assert.equal(componentSummary('dnscrypt-socks', { ...process, statename: 'STOPPED' }).tone, 'neutral');
  assert.equal(componentSummary('unbound-raw', { ...process, statename: 'FATAL' }).tone, 'error');
  assert.equal(cacheSaveSummary({ available: true, info: { rdb_last_bgsave_status: 'ok' } }).value, '上次保存成功');
  assert.equal(cacheSaveSummary({ available: true, info: { rdb_bgsave_in_progress: 1 } }).value, '正在保存');
});

test('stopped optional components never imply disabled configuration and names remain exact', () => {
  const stopped = { statename: 'STOPPED', pid: 0, start: 0, exitstatus: 0 };
  assert.equal(componentSummary('dnscrypt-socks', stopped).status, '已停止');
  assert.equal(componentSummary('unbound-raw', stopped).label, 'unbound-raw');
  assert.equal(componentSummary('redis', stopped).label, 'Redis');
  assert.equal(componentSummary('mosdns', { ...stopped, statename: 'EXITED' }).tone, 'neutral');
  assert.equal(componentSummary('mosdns', { ...stopped, statename: 'EXITED', exitstatus: 1 }).status, '异常退出');
});
