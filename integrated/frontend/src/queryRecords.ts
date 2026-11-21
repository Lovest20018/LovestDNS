import { suiteFetch } from './suiteApi';
import type { FilterDecision } from './filterApi';

export interface QueryEvent {
  id: string; sequence: number; observed_at: number; response_at: number | null;
  source_ip: string; source_port: number; entry_address: string; entry_port: number;
  component: string; transport: 'udp' | 'tcp'; domain: string; record_type: string;
  same_key_packets: number; rcode: string | null; answer_count: number | null;
  addresses: string[]; addresses_complete: boolean; truncated: boolean | null;
  elapsed_ms: number | null; outcome: 'response_observed' | 'no_response_observed';
  branch: null; cache_hit: null; timing_scope: 'passive_entry_packet_interval' | 'mosdns_entry_processing';
  filter?: FilterDecision;
}
export interface QueryRecording {
  enabled: boolean; active: boolean; phase: 'disabled' | 'starting' | 'recording' | 'unavailable';
  reason: string | null; sampled_at: number; retained_rows: number; database_bytes: number;
  counters: Record<string, number>; queue_depth: number;
  endpoints: { address: string; port: number; transport: string; component: string }[];
  limits: { retention_seconds: number; rows: number; storage_bytes: number; database_bytes: number };
  source?: 'linux_passive_entry_packets' | 'mosdns_entry_events';
}
export interface QueryRecords {
  instance_id: string; status: QueryRecording; events: QueryEvent[]; next_before: number | null;
  history_changed?: boolean;
  snapshot?: number;
}
export interface QueryBucket { start: number; end: number; count: number; coverage: 'complete' | 'partial' | 'unknown' }
export interface QueryRank { value: string; count: number }
export interface QuerySummary {
  instance_id: string; status: QueryRecording; start: number; end: number; snapshot: number;
  total: number; source_count: number; latency_samples: number; latency_scopes: Record<string, number>; p95_ms: number | null;
  buckets: QueryBucket[]; domains: QueryRank[]; sources: QueryRank[];
  evaluated: number; blocked: number; blocked_sources: QueryRank[];
}

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
export function recordingValid(value: unknown): value is QueryRecording {
  return object(value) && typeof value.enabled === 'boolean' && typeof value.active === 'boolean'
    && ['disabled', 'starting', 'recording', 'unavailable'].includes(String(value.phase))
    && number(value.sampled_at) && number(value.retained_rows) && number(value.database_bytes)
    && object(value.counters) && Object.values(value.counters).every(number) && Array.isArray(value.endpoints)
    && value.endpoints.every(e => object(e) && typeof e.address === 'string' && number(e.port) && typeof e.transport === 'string' && typeof e.component === 'string')
    && object(value.limits) && ['retention_seconds', 'rows', 'storage_bytes', 'database_bytes'].every(k => object(value.limits) && number(value.limits[k]));
}
export async function recentQueries(params: URLSearchParams, signal: AbortSignal): Promise<QueryRecords> {
  const value = await suiteFetch<QueryRecords>(`/api/v1/query-records?${params}`, { signal });
  if (typeof value.instance_id !== 'string' || !recordingValid(value.status) || !Array.isArray(value.events) || value.events.length > 100
    || !(value.next_before === null || number(value.next_before))
    || !value.events.every(e => object(e) && typeof e.id === 'string' && number(e.sequence) && number(e.observed_at)
      && typeof e.domain === 'string' && typeof e.source_ip === 'string' && typeof e.entry_address === 'string'
      && number(e.entry_port) && typeof e.record_type === 'string' && ['udp', 'tcp'].includes(String(e.transport))
      && ['response_observed', 'no_response_observed'].includes(String(e.outcome))
      && (e.elapsed_ms === null || number(e.elapsed_ms)) && (e.rcode === null || typeof e.rcode === 'string')
      && Array.isArray(e.addresses) && e.addresses.length <= 4 && e.addresses.every(a => typeof a === 'string')
      && typeof e.addresses_complete === 'boolean' && number(e.same_key_packets)
      && (e.filter === undefined || object(e.filter) && ['block', 'allow', 'pass'].includes(String(e.filter.action))
        && typeof e.filter.revision === 'string'
        && (e.filter.action === 'pass' || typeof e.filter.rule === 'string' && typeof e.filter.source === 'string')))) throw new Error('查询记录响应格式无效');
  return value;
}

export async function querySummary(hours: 1 | 24, signal: AbortSignal): Promise<QuerySummary> {
  const value = await suiteFetch<QuerySummary>('/api/v1/query-records/summary?hours=' + hours, { signal });
  const validRank = (items: unknown) => Array.isArray(items) && items.length <= 10
    && items.every(item => object(item) && typeof item.value === 'string' && number(item.count));
  if (!object(value) || typeof value.instance_id !== 'string' || !recordingValid(value.status)
    || ![value.start, value.end, value.snapshot, value.total, value.source_count, value.latency_samples,
      value.evaluated, value.blocked].every(number)
    || value.end - value.start !== hours * 3600 || !(value.p95_ms === null || number(value.p95_ms))
    || !object(value.latency_scopes) || !Object.values(value.latency_scopes).every(number)
    || !Array.isArray(value.buckets) || value.buckets.length !== (hours === 1 ? 12 : 24)
    || !value.buckets.every(b => object(b) && number(b.start) && number(b.end) && number(b.count)
      && ['complete', 'partial', 'unknown'].includes(String(b.coverage)))
    || ![value.domains, value.sources, value.blocked_sources].every(validRank)) throw new Error('解析概览响应格式无效');
  return value;
}
