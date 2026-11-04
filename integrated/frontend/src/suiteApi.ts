import type { DnsTestResult } from './api';

let browserCsrf = '';
export function setBrowserCsrf(value: string) { browserCsrf = value; }

export interface BrowserSession {
  authenticated: boolean; csrf_token?: string; must_change_password?: boolean; initial_password_file?: string | null;
}

export async function browserAuth(path: string, body?: Record<string, unknown>): Promise<BrowserSession> {
  const response = await fetch(`/auth/${path}`, {
    method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': browserCsrf },
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(20000),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error ?? '登录服务暂不可用');
  setBrowserCsrf(value.csrf_token ?? '');
  return value;
}

export class ApiError extends Error {
  status: number;
  requestId: string;
  constructor(message: string, status: number, requestId = '') {
    super(message); this.name = 'ApiError'; this.status = status; this.requestId = requestId;
  }
}

export async function suiteFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('Content-Type', 'application/json');
  if (browserCsrf && !['GET', 'HEAD', 'OPTIONS'].includes((init?.method ?? 'GET').toUpperCase())) headers.set('X-CSRF-Token', browserCsrf);
  const url = path.startsWith('/api/v1/') ? path : path.replace(/^\/api\//, '/api/v1/');
  const response = await fetch(url, { ...init, credentials: 'same-origin', headers, signal: init?.signal ?? AbortSignal.timeout(20000) });
  if (response.status === 401) window.dispatchEvent(new Event('lovestdns-auth-expired'));
  const requestId = response.headers.get('X-Request-ID') ?? '';
  const body = await response.json().catch(() => { throw new ApiError('API 返回内容无效，请检查后端地址和代理配置', response.status, requestId); });
  if (!response.ok) {
    const message = typeof body.error === 'string' ? body.error : body.error?.message ?? `HTTP ${response.status}`;
    throw new ApiError(message, response.status, body.meta?.request_id ?? requestId);
  }
  return body.data as T;
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.requestId ? ` · 请求 ${error.requestId}` : ''}`;
  return error instanceof Error ? error.message : String(error);
}

export interface RuntimeSnapshot {
  schema_version: 1; sampled_at: number; interval_seconds?: number; core_healthy: boolean; ready: boolean;
  error?: string;
  components: Record<string, { statename: string; pid: number; start: number; exitstatus: number }>;
  layers: Record<string, { available: boolean; counters?: Record<string, number>;
    qps?: number | null; cache_hit_ratio?: number | null; comparable?: boolean; error?: string;
    comparison_reason?: string; window_seconds?: number | null }>;
  freshness?: { age_seconds: number; max_age_seconds: number };
  redis?: { available: boolean; info?: Record<string, unknown>; error?: string };
  persistence?: { last_save_time?: number; last_save_status?: string; saving?: number;
    loading?: number; changes_since_save?: number; rdb_bytes?: number | null; disk_free_bytes?: number;
    warnings: string[]; storage_error?: string; scope: string };
}

export interface Capabilities {
  edition: 'integrated'; version: string; api_version: '1'; instance_id: string; name?: string; deployment?: 'native' | 'container';
  access_role: 'admin' | 'observer'; core: { image: string; source_commit: string };
  dns_defaults: { server: string; port: number }; record_types: string[];
  features: Record<string, boolean>; runtime_error?: string;
  permissions: { observe: boolean; diagnose: boolean; configure: boolean; maintain: boolean };
}

export type SuiteDnsResult = Omit<DnsTestResult, 'record_type'> & {
  record_type: string; debug?: { name: string; value: string; ttl: number }[];
};
export interface SuiteReport { kind: 'paopaodns-dns-report'; schema_version: 1; started_utc: string;
  method: string; tests: { label: string; result: SuiteDnsResult }[] }
export interface Revision { id: number; created_at: number; sha256: string; bytes: number; reason: string }
export interface ApplyPlan { sha256: string; inputs: Record<string, string>; candidate_sha256: string;
  candidate: string; notice: string }
export interface MaintenanceTask {
  id: string; action: 'apply_custom_mod' | 'save_redis_snapshot' | 'restart_dns_core';
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'; created: number; updated: number;
  result?: { message?: string; error?: string; state?: string; verified?: boolean;
    rollback?: { runtime_restored: boolean; service_running: boolean; error?: string };
    saved_at?: number; bytes?: number } | null;
}

export function bytes(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (value < 1024) return `${value} B`;
  return value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${(value / 1024 ** 3).toFixed(2)} GiB`;
}
export function when(value?: number) { return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '未采集'; }
export function downloadJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
