import type { MaintenanceTask } from './suiteApi';

export interface MaintenanceRequest {
  action: MaintenanceTask['action'];
  request_id: string;
  expected_sha256: string | null;
  created: number;
  retryRejected?: true;
}

const key = 'lovestdns-pending-maintenance';
let pending: { instance: string; request: MaintenanceRequest } | null = null;
try {
  const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
  const request = value?.request;
  if (typeof value?.instance === 'string' && request && /^[a-f0-9]{32}$/.test(request.request_id)
    && Number.isFinite(request.created) && ['restart_dns_core', 'apply_custom_mod', 'save_redis_snapshot'].includes(request.action)
    && (request.action === 'apply_custom_mod' ? /^[a-f0-9]{64}$/.test(request.expected_sha256) : request.expected_sha256 === null)) pending = value;
} catch { /* Use memory when storage is unavailable. */ }

export function pendingMaintenanceRequest(instance?: string) {
  return pending && pending.instance === instance ? pending.request : null;
}

export function rememberMaintenanceRequest(instance: string, request: MaintenanceRequest) {
  pending = { instance, request };
  try { sessionStorage.setItem(key, JSON.stringify(pending)); } catch { /* Memory remains available. */ }
}

export function createMaintenanceRequest(action: MaintenanceTask['action'], expected_sha256: string | null): MaintenanceRequest {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const request_id = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return { action, request_id, expected_sha256, created: Date.now() };
}

export function clearMaintenanceRequests() {
  pending = null;
  try { sessionStorage.removeItem(key); } catch { /* Memory has been cleared. */ }
}
