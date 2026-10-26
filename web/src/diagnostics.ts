import type { DiagnosticReport, DnsAnswer, DnsStatus, DnsTestResult } from './api';

export const DIAGNOSTIC_LIMIT = 2 * 1024 * 1024;
export const DNS_STATUS_LABELS: Record<DnsStatus, string> = {
  ok: '已解析', no_records: '无目标记录', dns_error: 'DNS 错误', incomplete: '应答不完整',
  timeout: '查询超时', network_error: '网络错误', invalid_response: '应答无效',
  invalid_input: '输入无效', probe_error: '探测失败',
};

export interface HostSnapshot {
  started_utc: string;
  container: string;
  elapsed_seconds?: number;
  stats_delta: Record<string, number>;
  stats_before: Record<string, unknown>;
  stats_after: Record<string, unknown>;
  observed_counters_reset: boolean;
  production_configs_unchanged: boolean | null;
}

export interface ImportedDiagnostic {
  report: DiagnosticReport;
  snapshot: HostSnapshot | null;
  original: unknown;
}

export function makeDiagnosticReport(tests: DiagnosticReport['tests']): DiagnosticReport {
  return {
    schema_version: 1, kind: 'paopaodns-dns-report', started_utc: new Date().toISOString(),
    method: '按需 DNS 探测，未清空缓存；解析成功不代表答案新鲜或网站可访问。', tests,
  };
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('报告对象格式无效');
  return value as Record<string, unknown>;
}

function text(value: unknown, max = 4096): string {
  if (typeof value !== 'string' || value.length > max) throw new Error('报告文本字段无效或过长');
  return value;
}

function number(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) {
    throw new Error('报告数值字段无效');
  }
  return value;
}

function timestamp(value: unknown): string {
  const result = text(value, 80);
  if (!Number.isFinite(Date.parse(result))) throw new Error('报告时间无效');
  return result;
}

function answers(value: unknown): DnsAnswer[] {
  if (!Array.isArray(value) || value.length > 512) throw new Error('报告应答列表无效');
  return value.map((entry) => {
    const item = object(entry);
    const ttl = number(item.ttl, 0, 4294967295);
    if (!Number.isInteger(ttl)) throw new Error('报告 TTL 无效');
    return {
      name: text(item.name, 253), type: text(item.type, 20), ttl,
      value: text(item.value), class: item.class === undefined ? 1 : number(item.class, 0, 65535),
    };
  });
}

function port(value: unknown): number {
  const result = number(value, 1, 65535);
  if (!Number.isInteger(result)) throw new Error('报告端口无效');
  return result;
}

function recordType(value: unknown): DnsTestResult['record_type'] {
  if (value !== 'A' && value !== 'AAAA' && value !== 'CNAME') throw new Error('报告记录类型无效');
  return value;
}

function matchingAnswers(records: DnsAnswer[], domain: string, type: DnsTestResult['record_type']): DnsAnswer[] {
  const normalizeName = (name: string) => name.toLowerCase().replace(/\.$/, '');
  let name = normalizeName(domain);
  const visited = new Set<string>();
  while (!visited.has(name)) {
    visited.add(name);
    const owned = records.filter((record) => record.class === 1 && normalizeName(record.name) === name);
    const matched = owned.filter((record) => record.type === type);
    if (matched.length) return matched;
    const alias = owned.find((record) => record.type === 'CNAME');
    if (!alias) break;
    name = normalizeName(alias.value);
  }
  return [];
}

function parseWebResult(value: unknown): DnsTestResult {
  const item = object(value);
  if (typeof item.available !== 'boolean' || typeof item.status !== 'string'
    || !Object.hasOwn(DNS_STATUS_LABELS, item.status)) throw new Error('报告查询状态无效');
  if (item.transport !== 'udp' && item.transport !== 'tcp') throw new Error('报告传输协议无效');
  if (!Array.isArray(item.results) || item.results.length > 512) throw new Error('报告结果列表无效');
  const result: DnsTestResult = {
    available: item.available, domain: text(item.domain, 253), record_type: recordType(item.record_type),
    server: text(item.server, 253), port: port(item.port), status: item.status as DnsStatus,
    transport: item.transport, answers: answers(item.answers), results: item.results.map((v) => text(v)),
    elapsed_ms: item.elapsed_ms === undefined ? undefined : number(item.elapsed_ms),
    rcode: item.rcode === undefined ? undefined : text(item.rcode, 30),
    error: item.error === undefined ? undefined : text(item.error),
    tcp_fallback: item.tcp_fallback === true, truncated: item.truncated === true,
    udp_truncated: item.udp_truncated === true, responded: item.responded === true,
    checked_at: item.checked_at === undefined ? undefined : timestamp(item.checked_at),
  };
  if (result.available !== (result.status === 'ok') || (result.available
    && (result.rcode !== 'NOERROR' || result.truncated
      || !matchingAnswers(result.answers, result.domain, result.record_type).length))) {
    throw new Error('报告成功状态与应答记录不一致');
  }
  return result;
}

// Legacy host snapshots contain dig output, not the Web UI result schema.
function parseHostQuery(value: unknown): DiagnosticReport['tests'][number] {
  const item = object(value);
  const domain = text(item.domain, 253);
  const records = answers(item.records);
  const type = recordType(item.record_type ?? 'A');
  const matched = matchingAnswers(records, domain, type);
  const rcode = text(item.status, 40);
  const truncated = item.truncated === true;
  const status: DnsStatus = item.exit !== 0 ? 'probe_error' : truncated ? 'incomplete' : rcode !== 'NOERROR'
    ? 'dns_error' : matched.length ? 'ok' : 'no_records';
  return {
    label: text(item.role, 80),
    result: {
      available: status === 'ok', domain, record_type: type,
      server: '127.0.0.1（宿主机采集的容器内部）', port: port(item.port),
      status, rcode, answers: records, results: matched.map((r) => r.value),
      transport: item.tcp === true ? 'tcp' : 'udp',
      truncated,
      elapsed_ms: item.dns_ms == null ? undefined : number(item.dns_ms),
      error: item.stderr ? text(item.stderr) : undefined,
    },
  };
}

export function parseDiagnosticFile(content: string): ImportedDiagnostic {
  if (new TextEncoder().encode(content).length > DIAGNOSTIC_LIMIT) throw new Error('报告不能超过 2 MB');
  const original: unknown = JSON.parse(content.replace(/^\uFEFF/, ''));
  const item = object(original);
  if (item.kind === 'paopaodns-dns-report' && item.schema_version === 1) {
    if (!Array.isArray(item.tests) || !item.tests.length || item.tests.length > 32) {
      throw new Error('报告必须包含 1–32 个查询结果');
    }
    return {
      original, snapshot: null,
      report: {
        kind: 'paopaodns-dns-report', schema_version: 1, started_utc: timestamp(item.started_utc),
        method: text(item.method), tests: item.tests.map((entry) => {
          const test = object(entry);
          return { label: text(test.label, 80), result: parseWebResult(test.result) };
        }),
      },
    };
  }
  if (!Array.isArray(item.queries) || !item.queries.length || item.queries.length > 32
    || typeof item.container !== 'string' || typeof item.method !== 'string') {
    throw new Error('请选择 WebUI 诊断报告或 paopaodns-health-snapshot.py 生成的 JSON');
  }
  const delta = object(item.stats_delta);
  const statsDelta: Record<string, number> = {};
  for (const [key, value] of Object.entries(delta)) {
    // Only known counters are rendered; other metadata remains in the original export.
    if (['total.num.queries', 'total.num.cachehits', 'total.num.cachemiss', 'total.num.prefetch',
      'total.num.expired', 'total.requestlist.exceeded', 'time.up'].includes(key)) {
      statsDelta[key] = number(value, -Number.MAX_SAFE_INTEGER);
    }
  }
  const started = timestamp(item.started_utc);
  return {
    original,
    report: {
      kind: 'paopaodns-dns-report', schema_version: 1, started_utc: started,
      method: text(item.method), tests: item.queries.map(parseHostQuery),
    },
    snapshot: {
      started_utc: started, container: text(item.container, 253),
      elapsed_seconds: item.elapsed_seconds === undefined ? undefined : number(item.elapsed_seconds),
      stats_delta: statsDelta, stats_before: object(item.stats_before), stats_after: object(item.stats_after),
      observed_counters_reset: item.observed_counters_reset === true || Object.values(statsDelta).some((v) => v < 0),
      production_configs_unchanged: typeof item.production_configs_unchanged === 'boolean'
        ? item.production_configs_unchanged : null,
    },
  };
}
