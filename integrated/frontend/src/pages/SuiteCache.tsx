import { Database } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { bytes, when } from '../suiteApi';
import { Section, Notice, Offline } from '../components/SuiteUI';
import SuiteMaintenance from '../components/SuiteMaintenance';
import './SuitePage.css';
export default function SuiteCache() {
  const { caps, runtime, paused, runtimeStale } = useSuite();
  if (!caps) return <Offline />;
  const info = runtime?.redis?.info;
  const keys = (db: string) => { const value = info?.[db]; return value && typeof value === 'object' && 'keys' in value ? String(value.keys) : '—'; };
  return <div className="suite-page"><p className="suite-muted">更新于 {when(runtime?.sampled_at)}</p>{(paused || runtimeStale) && <Notice>历史快照 · {paused ? '采集已暂停' : '数据过期'}</Notice>}
    <Section title="Redis" subtitle="共享缓存" icon={Database}><dl className="suite-kpi-list"><div><dt>used_memory</dt><dd>{bytes(info?.used_memory)}</dd></div><div><dt>maxmemory</dt><dd>{info?.maxmemory === 0 ? '未设置 Redis 上限' : bytes(info?.maxmemory)}</dd></div><div><dt>db0 Redis 键数</dt><dd>{keys('db0')}</dd></div><div><dt>db1 Redis 键数</dt><dd>{keys('db1')}</dd></div><div><dt>淘汰计数</dt><dd>{typeof info?.evicted_keys === 'number' ? info.evicted_keys : '—'}</dd></div></dl>{runtime?.redis?.error && <Notice tone="warning">{runtime.redis.error}</Notice>}</Section>
    <div className="suite-dashboard-grid">{(['recursive', 'forward'] as const).map((name) => { const layer = runtime?.layers[name]; return <Section key={name} title={name === 'recursive' ? 'unbound-raw' : 'unbound-forward'} subtitle="Unbound 缓存 · 与 Redis 分列" icon={Database}><dl className="suite-kpi-list"><div><dt>缓存命中率</dt><dd>{layer?.cache_hit_ratio == null ? '—' : `${(layer.cache_hit_ratio * 100).toFixed(1)}%`}</dd></div>{Object.entries(layer?.counters ?? {}).filter(([key]) => key.startsWith('mem.cache.')).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{bytes(value)}</dd></div>)}</dl>{layer && !layer.available && <p className="suite-muted">{layer.error ?? '未采集'}</p>}</Section>; })}</div>
    {caps.permissions.maintain ? <SuiteMaintenance caps={caps} runtime={runtime} mode="cache" /> : <Section title="RDB 持久化" icon={Database}><dl className="suite-kpi-list"><div><dt>大小</dt><dd>{bytes(runtime?.persistence?.rdb_bytes)}</dd></div><div><dt>最近保存</dt><dd>{when(runtime?.persistence?.last_save_time)}</dd></div><div><dt>结果</dt><dd>{runtime?.persistence?.last_save_status ?? '—'}</dd></div></dl></Section>}
  </div>;
}
