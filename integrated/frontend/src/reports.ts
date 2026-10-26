import type { SuiteDnsResult, SuiteReport } from './suiteApi';

export const RECORD_TYPES = ['A', 'AAAA', 'CNAME', 'NS', 'MX', 'TXT', 'SRV', 'PTR', 'SVCB', 'HTTPS'] as const;
export const REPORT_LIMIT = 2 * 1024 * 1024;
const STATUSES = ['ok', 'no_records', 'dns_error', 'incomplete', 'timeout', 'network_error', 'invalid_response', 'invalid_input', 'probe_error'];
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('报告结构无效');
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 4096): string {
  if (typeof value !== 'string' || value.length > limit) throw new Error('报告文本字段无效');
  return value;
}
function number(value: unknown, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > max) throw new Error('报告数值无效');
  return value;
}
function result(value: unknown): SuiteDnsResult {
  const data = object(value);
  const record = text(data.record_type, 10);
  if (!RECORD_TYPES.some((type) => type === record) || typeof data.available !== 'boolean'
      || !STATUSES.includes(String(data.status)) || !['udp', 'tcp'].includes(String(data.transport))) throw new Error('报告查询类型或状态无效');
  const port = number(data.port, 65535);
  if (!Number.isInteger(port) || port < 1) throw new Error('报告端口无效');
  if (!Array.isArray(data.answers) || data.answers.length > 512 || !Array.isArray(data.results) || data.results.length > 512) throw new Error('报告记录列表无效');
  const answers = data.answers.map((entry) => {
    const answer = object(entry); const ttl = number(answer.ttl, 4294967295);
    const rrclass = answer.class === undefined ? 1 : number(answer.class, 65535);
    if (!Number.isInteger(ttl) || !Number.isInteger(rrclass)) throw new Error('报告 TTL 或记录类别无效');
    return { name: text(answer.name, 253), type: text(answer.type, 20), value: text(answer.value), ttl, class: rrclass };
  });
  const debug = data.debug === undefined ? [] : (() => {
    if (!Array.isArray(data.debug) || data.debug.length > 512) throw new Error('报告诊断标记无效');
    return data.debug.map((entry) => { const mark = object(entry); return { name: text(mark.name, 253), value: text(mark.value), ttl: number(mark.ttl, 4294967295) }; });
  })();
  const parsed: SuiteDnsResult = { domain: text(data.domain, 253), record_type: record, server: text(data.server, 253), port,
    available: data.available, status: data.status as SuiteDnsResult['status'], transport: data.transport as 'udp' | 'tcp',
    answers, results: data.results.map((item) => text(item)), debug,
    rcode: data.rcode === undefined ? undefined : text(data.rcode, 30), error: data.error === undefined ? undefined : text(data.error),
    elapsed_ms: data.elapsed_ms === undefined ? undefined : number(data.elapsed_ms), truncated: data.truncated === true,
    tcp_fallback: data.tcp_fallback === true, udp_truncated: data.udp_truncated === true, responded: data.responded === true };
  if (data.checked_at !== undefined) {
    parsed.checked_at = text(data.checked_at, 80);
    if (!Number.isFinite(Date.parse(parsed.checked_at))) throw new Error('报告查询时间无效');
  }
  const normalize = (name: string) => name.toLowerCase().replace(/\.$/, '');
  let name = normalize(parsed.domain); const visited = new Set<string>(); let matched = false;
  while (!visited.has(name)) {
    visited.add(name);
    const owned = answers.filter((answer) => answer.class === 1 && normalize(answer.name) === name);
    if (owned.some((answer) => answer.type === record)) { matched = true; break; }
    const alias = owned.find((answer) => answer.type === 'CNAME');
    if (!alias) break;
    name = normalize(alias.value);
  }
  if (parsed.available !== (parsed.status === 'ok') || (parsed.available && (parsed.rcode !== 'NOERROR' || parsed.truncated || !matched))) throw new Error('报告成功状态与应答记录不一致');
  return parsed;
}

export function parseSuiteReport(content: string): SuiteReport {
  if (new TextEncoder().encode(content).length > REPORT_LIMIT) throw new Error('报告不能超过 2 MiB');
  const data = object(JSON.parse(content.replace(/^\uFEFF/, '')));
  if (data.kind !== 'paopaodns-dns-report' || data.schema_version !== 1 || !Array.isArray(data.tests) || data.tests.length < 1 || data.tests.length > 32) throw new Error('报告版本或类型无效');
  const started = text(data.started_utc, 80);
  if (!Number.isFinite(Date.parse(started))) throw new Error('报告时间无效');
  return { kind: 'paopaodns-dns-report', schema_version: 1, started_utc: started, method: text(data.method), tests: data.tests.map((entry) => { const test = object(entry); return { label: text(test.label, 80), result: result(test.result) }; }) };
}
