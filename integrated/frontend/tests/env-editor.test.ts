import assert from 'node:assert/strict';
import { test } from 'node:test';
import { editOverride, envOverrides } from '../src/envEditor.ts';

test('editing an override preserves comments, unrelated assignments, CRLF and trailing newline', () => {
  const input = '# operator note\r\nLOCAL_IP="192.0.2.1"\r\nCNAUTO="yes"\r\n# IPV6="no"\r\n';
  const output = editOverride(input, 'CNAUTO', 'no');
  assert.equal(output, input.replace('CNAUTO="yes"', 'CNAUTO="no"'));
  assert.deepEqual(envOverrides(output), { LOCAL_IP: '192.0.2.1', CNAUTO: 'no' });
  assert.equal(editOverride(input, 'IPV6', 'raw'), input + 'IPV6="raw"\r\n');
});

test('disabling and replacing duplicate assignments leaves one effective override and preserves original lines', () => {
  const input = 'CNAUTO="yes"\n# note\nCNAUTO="no"\n';
  assert.equal(envOverrides(editOverride(input, 'CNAUTO', 'yes')).CNAUTO, 'yes');
  const disabled = editOverride(input, 'CNAUTO', null);
  assert.equal(envOverrides(disabled).CNAUTO, undefined);
  assert.match(disabled, /# CNAUTO="yes"/);
  assert.match(disabled, /# CNAUTO="no"/);
  assert.deepEqual(envOverrides('SERVER_IP=""\n'), { SERVER_IP: '' });
});
