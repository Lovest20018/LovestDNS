import type { RuntimeSnapshot } from './suiteApi';

export interface TrendSample { at: number; recursive: number | null; forward: number | null;
  recursiveHit: number | null; forwardHit: number | null; memory: number | null }
export const TREND_LIMIT = 360;
export const TREND_SECONDS = 1800;
const nonnegative = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;

export function appendTrend(previous: TrendSample[], runtime: RuntimeSnapshot | null, now: number): TrendSample[] {
  const at = runtime?.sampled_at ?? now;
  const last = previous.at(-1);
  if (last && at <= last.at) return previous;
  const layer = (name: string, field: 'qps' | 'cache_hit_ratio') => {
    const item = runtime?.layers[name];
    const value = item?.available && item?.comparable ? nonnegative(item[field]) : null;
    return field === 'cache_hit_ratio' && value !== null && value > 1 ? null : value;
  };
  const sample: TrendSample = { at, recursive: layer('recursive', 'qps'), forward: layer('forward', 'qps'),
    recursiveHit: layer('recursive', 'cache_hit_ratio'), forwardHit: layer('forward', 'cache_hit_ratio'),
    memory: runtime?.redis?.available ? nonnegative(runtime.redis.info?.used_memory) : null };
  return [...previous.filter((point) => point.at > at - TREND_SECONDS), sample].slice(-TREND_LIMIT);
}

export function trendSegments(samples: TrendSample[], key: Exclude<keyof TrendSample, 'at'>): TrendSample[][] {
  const segments: TrendSample[][] = [];
  let active: TrendSample[] = [];
  for (const sample of samples) {
    if (sample[key] === null || (active.length > 0 && sample.at - active.at(-1)!.at > 15)) {
      if (active.length) segments.push(active);
      active = [];
    }
    if (sample[key] !== null) active.push(sample);
  }
  if (active.length) segments.push(active);
  return segments;
}
