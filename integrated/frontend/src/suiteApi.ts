import type { DnsTestResult } from './api';

let browserCsrf = '';
let authExpired = false;
export function setBrowserCsrf(value: string) { browserCsrf = value; if (value) authExpired = false; }
function expireSession() {
  if (authExpired) return;
  authExpired = true;
  window.dispatchEvent(new Event('lovestdns-auth-expired'));
}

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
  code: string;
  retryable: boolean;
  details: unknown;
  constructor(message: string, status: number, requestId = '', code = '', details?: unknown) {
    super(message); this.name = 'ApiError'; this.status = status; this.requestId = requestId;
    this.code = code; this.retryable = !['SAVE_UNCONFIRMED', 'RESULT_UNCONFIRMED'].includes(code) && (status === 429 || status === 503 || code === 'TIMEOUT'); this.details = details;
  }
}

export async function suiteFetch<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('Content-Type', 'application/json');
  if (browserCsrf && !['GET', 'HEAD', 'OPTIONS'].includes((init?.method ?? 'GET').toUpperCase())) headers.set('X-CSRF-Token', browserCsrf);
  const url = path.startsWith('/api/v1/') ? path : path.replace(/^\/api\//, '/api/v1/');
  const deadline = AbortSignal.timeout(init?.timeoutMs ?? 20000);
  let response: Response;
  const method = (init?.method ?? 'GET').toUpperCase();
  try {
    response = await fetch(url, { ...init, cache: 'no-store', credentials: 'same-origin', headers,
      signal: init?.signal ? AbortSignal.any([init.signal, deadline]) : deadline });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      const mutation = !['GET', 'HEAD', 'OPTIONS'].includes(method) && !/\/dns-(?:test|compare)$/.test(url);
      throw new ApiError(mutation ? '操作响应超时，结果待确认。请先重新读取文件或任务状态，再决定是否重试。' : '读取超时，请重试。', 0, '', mutation ? 'RESULT_UNCONFIRMED' : 'TIMEOUT');
    }
    throw error;
  }
  if (response.status === 401) expireSession();
  const requestId = response.headers.get('X-Request-ID') ?? '';
  const body = await response.json().catch(() => { throw new ApiError('API 返回内容无效，请检查后端地址和代理配置', response.status, requestId); });
  if (!response.ok) {
    const failure = body && typeof body === 'object' ? body : {};
    const message = typeof failure.error === 'string' ? failure.error : failure.error?.message ?? `HTTP ${response.status}`;
    throw new ApiError(message, response.status, failure.meta?.request_id ?? requestId, failure.error?.code, failure.error?.details);
  }
  if (!body || typeof body !== 'object' || !Object.hasOwn(body, 'data') || body.data == null || body.meta?.api_version !== '1') {
    throw new ApiError('API 响应结构无效，请检查服务版本', response.status, requestId, 'INVALID_RESPONSE');
  }
  validatePayload(url, body.data, requestId, method);
  return body.data as T;
}

function validatePayload(url: string, data: unknown, requestId: string, method: string) {
  const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
  const object = data && typeof data === 'object' && !Array.isArray(data) ? data as Record<string, unknown> : null;
  let valid = !!object;
  if (object && url.endsWith('/capabilities')) {
    const permissions = object.permissions as Record<string, unknown> | undefined;
    valid = object.api_version === '1' && typeof object.instance_id === 'string' && !!permissions && ['observe', 'diagnose', 'configure', 'maintain'].every((key) => typeof permissions[key] === 'boolean') && !!object.features && !!object.core && !!object.dns_defaults;
    if (valid && object.installation !== undefined) {
      const installed = object.installation;
      valid = record(installed) && ['unavailable', 'native_service_environment'].includes(String(installed.source))
        && ['service_name', 'control_command', 'config_file', 'data_dir', 'state_dir', 'release_path'].every(key => installed[key] === null || typeof installed[key] === 'string')
        && record(installed.receipt) && ['missing', 'mismatch', 'recorded'].includes(String(installed.receipt.status))
        && ['archive_sha256', 'manifest_sha256'].every(key => installed.receipt !== null && record(installed.receipt) && (installed.receipt[key] === null || typeof installed.receipt[key] === 'string'))
        && (installed.receipt.installed_at === null || (typeof installed.receipt.installed_at === 'number' && Number.isFinite(installed.receipt.installed_at)));
    }
    if (valid && object.provenance !== undefined) {
      const provenance = object.provenance;
      valid = record(provenance) && provenance.source === 'installed_metadata' && provenance.process_files_verified === false
        && record(provenance.application) && record(provenance.frontend) && record(provenance.base_package)
        && ['application', 'frontend', 'base_package'].every(key => record(provenance[key]) && Object.values(provenance[key]).every(value => value === null || typeof value === 'string' || typeof value === 'boolean'));
    }
  }
  if (object && url.endsWith('/status')) valid = record(object.env) && Object.values(object.env).every((value) => typeof value === 'string') && record(object.files) && Object.values(object.files).every((value) => typeof value === 'boolean');
  if (object && url.endsWith('/runtime')) valid = object.schema_version === 1 && typeof object.sampled_at === 'number' && typeof object.ready === 'boolean' && typeof object.core_healthy === 'boolean' && record(object.components) && Object.values(object.components).every((value) => record(value) && typeof value.statename === 'string') && record(object.layers) && Object.values(object.layers).every((value) => record(value) && typeof value.available === 'boolean');
  if (valid && object && url.endsWith('/runtime') && object.dns != null) {
    const dns = object.dns;
    valid = record(dns) && dns.source === 'linux_process_sockets' && dns.scope === 'service_network_namespace'
      && ['automatic', 'manual', 'unknown'].includes(String(dns.mode)) && [null, 'mosdns', 'unbound-raw'].includes(dns.entry_component as string | null)
      && dns.client_path_verified === false && record(dns.listeners) && Object.values(dns.listeners).every((item) => record(item)
        && typeof item.available === 'boolean' && Array.isArray(item.endpoints) && item.endpoints.length <= 64 && item.endpoints.every((endpoint) => record(endpoint)
          && typeof endpoint.address === 'string' && typeof endpoint.port === 'number' && Number.isInteger(endpoint.port) && endpoint.port > 0 && endpoint.port < 65536
          && ['udp', 'tcp'].includes(String(endpoint.transport)) && [4, 6].includes(Number(endpoint.family)) && typeof endpoint.wildcard === 'boolean'));
  }
  if (object && url.endsWith('/config/state')) valid = Array.isArray(object.files) && object.files.every((row) => record(row)
    && typeof row.filename === 'string' && typeof row.revision === 'string' && ['unknown', 'recorded', 'changed'].includes(String(row.state)));
  if (object && /\/file\//.test(url)) valid = typeof object.revision === 'string' && (method !== 'GET' || typeof object.content === 'string');
  if (object && /\/history\/[^/]+$/.test(url)) valid = Array.isArray(object.revisions);
  if (object && url.endsWith('/maintenance/tasks') && 'tasks' in object) valid = Array.isArray(object.tasks);
  if (object && /\/dns-test$/.test(url)) valid = Array.isArray(object.answers) && typeof object.available === 'boolean';
  if (object && /\/dns-compare$/.test(url)) valid = Array.isArray(object.tests) && object.tests.length > 0;
  if (!valid) throw new ApiError('API 数据格式无效，请检查服务版本', 200, requestId, 'INVALID_RESPONSE');
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.requestId ? ` · 请求 ${error.requestId}` : ''}`;
  return error instanceof Error ? error.message : String(error);
}

export async function externalApiSetting(enabled?: boolean): Promise<boolean> {
  const response = await fetch('/auth/external-api', {
    method: enabled === undefined ? 'GET' : 'PUT', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': browserCsrf },
    body: enabled === undefined ? undefined : JSON.stringify({ enabled }), signal: AbortSignal.timeout(15000),
  });
  if (response.status === 401) expireSession();
  const body = await response.json();
  if (!response.ok) throw new Error(body.error ?? '无法更新外部 API 设置');
  if (typeof body.enabled !== 'boolean') throw new Error('外部 API 设置返回内容无效');
  return body.enabled;
}

export interface SoftwareRelease {
  version: string; archive_sha256: string; archive_bytes: number;
  prerelease: boolean; published_at: string; url: string; notes: string;
}
export type SoftwareUpdatePhase = 'idle' | 'queued' | 'checking' | 'ready' | 'downloading' | 'verifying' | 'installing' | 'succeeded' | 'failed';
export interface SoftwareUpdateStatus {
  schema_version: 1; supported: boolean; current_version: string; reason?: string;
  phase?: SoftwareUpdatePhase; job_id?: string | null; action?: 'check' | 'apply' | null;
  release?: SoftwareRelease | null; checked_at?: number | null; message?: string;
  installed_version?: string; previous_version_healthy?: boolean; healthy?: boolean;
}
export async function softwareUpdate(body?: { action: 'check' } | { action: 'apply'; version: string; archive_sha256: string }, signal?: AbortSignal): Promise<SoftwareUpdateStatus> {
  const deadline = AbortSignal.timeout(15000);
  const response = await fetch('/auth/software-update', {
    method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': browserCsrf },
    body: body ? JSON.stringify(body) : undefined, signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
  });
  if (response.status === 401) expireSession();
  const value = await response.json();
  if (!response.ok) throw new Error(value.error ?? '无法读取软件更新状态');
  const release = value?.release;
  const releaseValid = release == null || (typeof release === 'object'
    && typeof release.version === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$/.test(release.version)
    && typeof release.archive_sha256 === 'string' && /^[a-f0-9]{64}$/.test(release.archive_sha256)
    && typeof release.archive_bytes === 'number' && Number.isSafeInteger(release.archive_bytes) && release.archive_bytes > 0
    && typeof release.prerelease === 'boolean' && typeof release.notes === 'string'
    && typeof release.published_at === 'string'
    && release.url === `https://github.com/Lovest20018/LovestDNS/releases/tag/v${release.version}`);
  if (!value || value.schema_version !== 1 || typeof value.supported !== 'boolean' || typeof value.current_version !== 'string'
      || (value.reason !== undefined && typeof value.reason !== 'string')
      || (value.supported && (!releaseValid || (value.message !== undefined && typeof value.message !== 'string')
          || !['idle', 'queued', 'checking', 'ready', 'downloading', 'verifying', 'installing', 'succeeded', 'failed'].includes(value.phase)))) {
    throw new Error('软件更新状态格式无效');
  }
  return value;
}

export interface RuntimeSnapshot {
  schema_version: 1; sampled_at: number; interval_seconds?: number; core_healthy: boolean; ready: boolean;
  error?: string;
  dns?: {
    source: 'linux_process_sockets'; scope: 'service_network_namespace';
    mode: 'automatic' | 'manual' | 'unknown'; entry_component: 'mosdns' | 'unbound-raw' | null;
    client_path_verified: false;
    listeners: Record<string, { available: boolean; reason: 'not_running' | 'unconfirmed' | null;
      pid?: number; start_ticks?: string; endpoints: DnsListenerEndpoint[] }>;
  };
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

export interface DnsListenerEndpoint {
  address: string; port: number; transport: 'udp' | 'tcp'; family: 4 | 6; wildcard: boolean;
}

export interface Capabilities {
  edition: 'integrated'; version: string; api_version: '1'; instance_id: string; name?: string; deployment?: 'native' | 'container';
  access_role: 'admin' | 'observer'; core: { image: string; source_commit: string; components?: { selected: string[]; versions: Record<string, string>; lock_sha256: string } | null };
  dns_defaults: { server: string; port: number }; record_types: string[];
  features: Record<string, boolean>; runtime_error?: string;
  maintenance?: { available: boolean; phase: string };
  permissions: { observe: boolean; diagnose: boolean; configure: boolean; maintain: boolean };
  installation?: Installation;
  provenance?: {
    source: 'installed_metadata';
    application: { source_commit: string | null; source_sha256: string | null; method: string | null };
    frontend: { source_commit: string | null; source_sha256: string | null; manifest_sha256: string | null;
      dependency_mode: string | null; working_tree_modified: boolean | null };
    base_package: { version: string | null; archive_sha256: string | null };
    process_files_verified: false;
  };
}

export interface Installation {
  source: 'unavailable' | 'native_service_environment';
  service_name: string | null; control_command: string | null; config_file: string | null;
  data_dir: string | null; state_dir: string | null; release_path: string | null;
  receipt: { status: 'missing' | 'mismatch' | 'recorded'; archive_sha256: string | null;
    manifest_sha256: string | null; installed_at: number | null };
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
