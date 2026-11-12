import type { RuntimeSnapshot } from './suiteApi';

export interface ConfigurationVersions { files: { filename: string; revision: string }[] }
const DNS_PROCESSES = ['mosdns', 'unbound-raw', 'unbound-forward', 'dnscrypt', 'dnscrypt-socks', 'redis'];
export function processFingerprint(runtime: RuntimeSnapshot | null) {
  if (!runtime || runtime.error || !runtime.ready) return null;
  if (DNS_PROCESSES.some((name) => {
    const process = runtime.components[name];
    return !process || !process.statename || !Number.isInteger(process.pid) || process.pid < 0
      || !Number.isFinite(process.start) || process.start < 0
      || (process.statename === 'RUNNING' && (process.pid === 0 || process.start === 0));
  })) return null;
  return JSON.stringify(DNS_PROCESSES.map((name) => {
    const process = runtime.components[name];
    return [name, process.statename, process.pid, process.start];
  }));
}
export function configurationFingerprint(value: ConfigurationVersions) {
  return JSON.stringify(value.files.map(({ filename, revision }) => [filename, revision]).sort(([a], [b]) => a.localeCompare(b)));
}
