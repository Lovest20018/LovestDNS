import type { Capabilities, DnsListenerEndpoint, RuntimeSnapshot } from './suiteApi';

export function dnsAddress(address: string, port: number) {
  return `${address.includes(':') ? `[${address}]` : address}:${port}`;
}

export function entryListeners(runtime: RuntimeSnapshot | null) {
  const dns = runtime?.dns;
  if (!dns?.entry_component) return [];
  const observation = dns.listeners[dns.entry_component];
  return observation?.available ? observation.endpoints : [];
}

export interface DiagnosticTarget { id: string; label: string; server: string; port: number; transport: 'udp' | 'tcp' }
export function diagnosticTargets(caps: Capabilities | null, runtime: RuntimeSnapshot | null): DiagnosticTarget[] {
  if (!caps) return [];
  const result: DiagnosticTarget[] = [{ id: 'default', label: '默认诊断目标', ...caps.dns_defaults, transport: 'udp' }];
  for (const [component, label] of [['mosdns', '分流入口'], ['unbound-raw', '递归通道'], ['unbound-forward', '转发通道']]) {
    const observation = runtime?.dns?.listeners[component];
    if (!observation?.available) continue;
    // Wildcard binds become loopback query destinations inside the service;
    // they are never advertised as LAN addresses.
    for (const endpoint of observation.endpoints) {
      if (!validTarget(endpoint)) continue;
      const server = endpoint.wildcard ? endpoint.family === 6 ? '::1' : '127.0.0.1' : endpoint.address;
      if (/^fe[89ab]/i.test(server) && endpoint.family === 6) continue;
      const id = `${component}|${server}|${endpoint.port}|${endpoint.transport}`;
      if (result.some((item) => item.id === id)) continue;
      result.push({ id, label: `${label} · ${dnsAddress(server, endpoint.port)} / ${endpoint.transport.toUpperCase()}`,
        server, port: endpoint.port, transport: endpoint.transport });
    }
  }
  return result;
}

function validTarget(endpoint: DnsListenerEndpoint) {
  return endpoint.address && Number.isInteger(endpoint.port) && endpoint.port > 0 && endpoint.port < 65536
    && ['udp', 'tcp'].includes(endpoint.transport) && [4, 6].includes(endpoint.family);
}
