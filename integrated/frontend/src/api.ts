import { suiteFetch as apiFetch } from './suiteApi';

export interface SystemStatus {
  data_dir: string;
  data_readable: boolean;
  data_writable: boolean;
  auth_enabled: boolean;
  env: Record<string, string>;
  files: Record<string, boolean>;
  files_info: Record<string, {
    exists: boolean;
    auto_reload: boolean;
    requires_reload: boolean;
    requires_restart: boolean;
    watched_now: boolean;
    condition: string;
  }>;
}

export interface FileContent {
  revision?: string;
  filename: string;
  content: string;
  auto_reload: boolean;
  requires_reload: boolean;
  requires_restart: boolean;
  watched_now: boolean;
  condition: string;
}

export interface SaveResult {
  revision?: string;
  message: string;
  filename: string;
  auto_reload: boolean;
  requires_reload: boolean;
  requires_restart: boolean;
  watched_now: boolean;
  condition: string;
}

export interface DnsAnswer {
  name: string;
  type: string;
  ttl: number;
  value: string;
  class?: number;
}

export interface DnsTestResult {
  available: boolean;
  domain: string;
  record_type: 'A' | 'AAAA' | 'CNAME';
  server: string;
  port: number;
  elapsed_ms?: number;
  rcode?: string;
  status?: DnsStatus;
  responded?: boolean;
  transport?: 'udp' | 'tcp';
  tcp_fallback?: boolean;
  udp_truncated?: boolean;
  truncated?: boolean;
  checked_at?: string;
  answers: DnsAnswer[];
  results: string[];
  error?: string;
}

export type DnsStatus = 'ok' | 'no_records' | 'dns_error' | 'incomplete' | 'timeout'
  | 'network_error' | 'invalid_response' | 'invalid_input' | 'probe_error';

export interface DnsTarget {
  label: string;
  server: string;
  port: number;
  transport?: 'udp' | 'tcp';
}

export interface DiagnosticReport {
  schema_version: 1;
  kind: 'paopaodns-dns-report';
  started_utc: string;
  method: string;
  tests: { label: string; result: DnsTestResult }[];
}

export interface HealthCheckResult {
  pass: boolean;
  server: string;
  port: number;
  error?: string;
  tests: Record<string, {
    domain: string;
    resolved: boolean;
    result?: DnsTestResult;
  }>;
}

export async function getStatus(): Promise<SystemStatus> {
  return apiFetch<SystemStatus>('/api/status');
}

export async function getEnv(): Promise<Record<string, string>> {
  return apiFetch<Record<string, string>>('/api/env');
}

export async function readFile(filename: string, signal?: AbortSignal): Promise<FileContent> {
  return apiFetch<FileContent>(`/api/file/${encodeURIComponent(filename)}`, { signal });
}

export async function writeFile(filename: string, content: string, expectedRevision?: string): Promise<SaveResult> {
  return apiFetch<SaveResult>(`/api/file/${filename}`, {
    method: 'PUT',
    body: JSON.stringify({ content, expected_revision: expectedRevision }),
  });
}

export async function runDnsTest(params: {
  domain: string;
  record_type: 'A' | 'AAAA' | 'CNAME';
  server?: string;
  port?: number;
  transport?: 'udp' | 'tcp';
}): Promise<DnsTestResult> {
  return apiFetch<DnsTestResult>('/api/dns-test', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export async function compareDns(params: {
  domain: string;
  record_type: 'A' | 'AAAA' | 'CNAME';
  targets: DnsTarget[];
}): Promise<DiagnosticReport> {
  return apiFetch<DiagnosticReport>('/api/dns-compare', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

export async function runHealthCheck(params?: { server?: string; port?: number }): Promise<HealthCheckResult> {
  const query = new URLSearchParams();
  if (params?.server) query.set('server', params.server);
  if (params?.port) query.set('port', String(params.port));
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return apiFetch<HealthCheckResult>(`/api/health-check${suffix}`);
}

export function setToken(token: string): void {
  void token; // Legacy pages are compiled but integrated auth uses HttpOnly sessions.
}

export function getStoredToken(): string {
  return '';
}
