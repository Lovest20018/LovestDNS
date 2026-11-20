import { suiteFetch } from './suiteApi';

export interface FilterDecision {
  action: 'block' | 'allow' | 'pass';
  revision: string;
  id?: string;
  source?: string;
  rule?: string;
  until?: number;
}
interface RuleCounts { valid: number; duplicates: number; ignored: number; unsupported: number; invalid: number; protected: number }
export interface FilterSource {
  id: string; name: string; host: string; enabled: boolean; interval: number; updated: number; parse: RuleCounts;
}
export interface FilterState {
  enabled: boolean; local_rules: string; sources: FilterSource[]; allow: Record<string, number | null>; rules: number;
  revision: string; mode: 'mosdns' | 'recursive' | 'unavailable'; available: boolean; live_revision: string | null; live_enabled: boolean;
  draft: { id: string; name: string; host: string; parse: RuleCounts & { issues: {
    line: number; rule: string; kind: string; reason: string; truncated: boolean;
  }[] } } | null;
  jobs: { id: string; phase: 'pending' | 'succeeded' | 'failed'; message: string; action: string }[];
}
export type FilterAction =
  | { action: 'configure'; enabled: boolean; local_rules: string }
  | { action: 'preview'; name: string; url: string; interval: number }
  | { action: 'apply_preview'; preview: string; partial: boolean }
  | { action: 'refresh'; id: string }
  | { action: 'subscription'; id: string; enabled: boolean; interval: number; remove: boolean }
  | { action: 'allow'; domain: string; mode: 'temporary' | 'permanent' | 'remove' };
export type FilterCheck = Omit<FilterDecision, 'action'> & { action: FilterDecision['action'] | 'disabled'; domain: string };

export const filterLabels = { block: '拦截', allow: '放行', pass: '继续解析', disabled: '过滤已关闭' };

export function filterState(signal?: AbortSignal) {
  return suiteFetch<FilterState>('/api/v1/dns-filter', { signal });
}

export function checkFilter(domain: string, signal?: AbortSignal) {
  return suiteFetch<FilterCheck>('/api/v1/dns-filter/check?' + new URLSearchParams({ domain }), { signal });
}

export async function applyFilter(action: FilterAction, revision: string, signal: AbortSignal) {
  const { id } = await suiteFetch<{ id: string }>('/api/v1/dns-filter/actions', {
    method: 'POST', signal, body: JSON.stringify({ ...action, revision }),
  });
  const until = Date.now() + 45000;
  while (Date.now() < until) {
    const state = await filterState(signal);
    const job = state.jobs.find(item => item.id === id);
    if (!job) throw new Error('过滤任务状态已失效，请刷新活动版本');
    if (job.phase !== 'pending') return { state, job };
    await new Promise(resolve => window.setTimeout(resolve, 750));
    signal.throwIfAborted();
  }
  throw new Error('等待过滤任务超时，请刷新活动版本');
}
