import assert from 'node:assert/strict';
import test from 'node:test';
import { diagnosticTargets, dnsAddress, entryListeners } from '../src/dnsListeners.ts';
import type { Capabilities, RuntimeSnapshot } from '../src/suiteApi.ts';

const caps = { dns_defaults: { server: '127.0.0.1', port: 15453 } } as Capabilities;
test('missing observations do not invent internal ports or LAN entry', () => {
  assert.deepEqual(entryListeners(null), []);
  assert.equal(diagnosticTargets(caps, null).length, 1);
  assert.equal(diagnosticTargets(caps, null)[0].port, 15453);
  assert.equal(dnsAddress('2001:db8::1', 53), '[2001:db8::1]:53');
});
test('presets use owned listeners with protocol and wildcard loopback conversion', () => {
  const runtime = { dns: { entry_component: 'mosdns', listeners: {
    mosdns: { available: true, endpoints: [{ address: '::', port: 15453, transport: 'tcp', family: 6, wildcard: true }] },
    'unbound-raw': { available: false, endpoints: [{ address: '127.0.0.1', port: 5301, transport: 'udp', family: 4, wildcard: false }] },
    'unbound-forward': { available: true, endpoints: [{ address: 'fe80::1', port: 5304, transport: 'udp', family: 6, wildcard: false }] },
  } } } as unknown as RuntimeSnapshot;
  assert.equal(entryListeners(runtime)[0].address, '::');
  const targets = diagnosticTargets(caps, runtime);
  assert.equal(targets.length, 2);
  assert.equal(targets[1].server, '::1');
  assert.equal(targets[1].transport, 'tcp');
});
