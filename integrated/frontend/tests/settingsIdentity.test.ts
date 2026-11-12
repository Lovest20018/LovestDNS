import assert from 'node:assert/strict';
import test from 'node:test';
import { nativeMaintenanceCommands } from '../src/settingsIdentity.ts';
import type { Installation } from '../src/suiteApi.ts';

const installed = (command: string, source = 'native_service_environment') => ({ source, control_command: command } as Installation);

test('named instance commands use only the installed control path', () => {
  const commands = nativeMaintenanceCommands(installed('/usr/local/sbin/lovestdns-validationctl'));
  assert(commands?.includes('sudo /usr/local/sbin/lovestdns-validationctl health'));
  assert(!commands?.includes('sudo lovestdnsctl'));
  assert(nativeMaintenanceCommands(installed('/usr/local/sbin/lovestdnsctl'))?.includes('sudo /usr/local/sbin/lovestdnsctl status'));
});

test('unknown or unsafe instance identities cannot offer a default or injected command', () => {
  for (const input of [undefined, installed('/usr/local/sbin/lovestdnsctl', 'unavailable'),
    installed('/usr/local/sbin/lovestdnsctl; id'), installed('lovestdnsctl'), installed('/tmp/lovestdnsctl')]) {
    assert.equal(nativeMaintenanceCommands(input), null);
  }
});
