import type { RuntimeSnapshot } from './suiteApi';

export const MAX_RUNTIME_AGE_SECONDS = 25;

export function runtimeLifetimeMs(sample: RuntimeSnapshot, roundTripMs = 0, now = Date.now()): number {
  if (!Number.isFinite(sample.sampled_at) || !Number.isFinite(roundTripMs) || roundTripMs < 0) throw new Error('运行样本时间无效');
  // API freshness is measured by the collector host. Browser and server clocks
  // need not agree; use monotonic time after receipt to expire the sample.
  const age = sample.freshness ? sample.freshness.age_seconds : now / 1000 - sample.sampled_at;
  const maximum = sample.freshness ? Math.min(MAX_RUNTIME_AGE_SECONDS, sample.freshness.max_age_seconds) : MAX_RUNTIME_AGE_SECONDS;
  const remaining = (maximum - age) * 1000 - roundTripMs;
  if (!Number.isFinite(age) || age < 0 || !Number.isFinite(maximum) || maximum <= 0 || !Number.isFinite(remaining) || remaining <= 0) throw new Error('运行样本已过期或采样时间无效');
  return remaining;
}
