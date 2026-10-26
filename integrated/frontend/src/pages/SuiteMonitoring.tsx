import { Activity, Database, RefreshCw, Server } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { bytes, when } from '../suiteApi';
import { Empty, Section, Status, Notice, Stat } from '../components/SuiteUI';
import TrendChart from '../components/TrendChart';
import './SuitePage.css';

export default function SuiteMonitoring() {
  const { caps, runtime, samples, paused, setPaused, refresh } = useSuite();
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后会自动刷新组件和分层指标。</Empty>;
  const layers = runtime?.layers ?? {};
  return <div className="suite-page">
    <div className="suite-toolbar"><div><p className="suite-lead">数据来自集成版采集器，每 5 秒更新一次。</p><p className="suite-muted">采样时间：{when(runtime?.sampled_at)} · {paused ? '自动刷新已暂停' : '自动刷新中'}</p></div><div className="suite-button-row"><button className="btn btn-secondary btn-sm" onClick={() => setPaused(!paused)}>{paused ? '恢复自动刷新' : '暂停自动刷新'}</button><button className="btn btn-secondary btn-sm" onClick={() => void refresh()}><RefreshCw size={14} />立即采样</button></div></div>
    {runtime?.error && <Notice tone="error">采集器返回错误：{runtime.error}</Notice>}
    <div className="suite-stat-grid"><Stat label="核心健康" value={<Status value={runtime?.core_healthy ? 'good' : runtime ? 'warning' : 'neutral'} label={runtime?.core_healthy ? '运行正常' : runtime ? '组件异常' : '不可用'} />} detail="组件状态与 ready 标记" icon={Server} /><Stat label="Redis 内存" value={bytes(runtime?.redis?.info?.used_memory)} detail={`版本 ${String(runtime?.redis?.info?.redis_version ?? '未采集')}`} icon={Database} /><Stat label="RDB 状态" value={String(runtime?.redis?.info?.rdb_last_bgsave_status ?? '未采集')} detail={`变更 ${String(runtime?.redis?.info?.rdb_changes_since_last_save ?? '—')}`} icon={Database} /></div>
    <div className="suite-dashboard-grid"><Section title="查询速率" subtitle="只代表对应 Unbound 层处理的请求" icon={Activity}><div className="suite-chart-pair"><TrendChart samples={samples} field="recursive" label="递归侧 QPS" unit="queries / second" /><TrendChart samples={samples} field="forward" label="转发侧 QPS" unit="queries / second" color="var(--accent-purple)" /></div></Section><Section title="缓存命中率" subtitle="计数器重置或采集不可比较时显示断点" icon={Database}><div className="suite-chart-pair"><TrendChart samples={samples} field="recursiveHit" label="递归侧" unit="ratio" color="var(--accent-teal)" /><TrendChart samples={samples} field="forwardHit" label="转发侧" unit="ratio" color="var(--accent-amber)" /></div></Section></div>
    <div className="suite-dashboard-grid"><Section title="递归侧 Unbound" subtitle="独立统计，不等于终端总流量" icon={Server}><Layer layer={layers.recursive} /></Section><Section title="转发侧 Unbound" subtitle="CNAUTO 分流时使用" icon={Server}><Layer layer={layers.forward} /></Section></div>
    <Section title="组件状态" subtitle="进程重启后计数器不会跨周期计算速率" icon={Activity}><div className="suite-component-grid">{Object.entries(runtime?.components ?? {}).map(([name, value]) => <div className="suite-component" key={name}><i className={value.statename === 'RUNNING' ? 'is-good' : 'is-warning'} /><code>{name}</code><span>{value.statename} · PID {value.pid || '—'}</span></div>)}</div></Section>
  </div>;
}
function Layer({ layer }: { layer?: { available: boolean; counters?: Record<string, number>; qps?: number | null; cache_hit_ratio?: number | null; comparable?: boolean; error?: string } }) {
  if (!layer?.available) return <Empty title="本层数据不可用">{layer?.error ?? '等待组件启动或下一次采样'}</Empty>;
  return <dl className="suite-kpi-list"><div><dt>QPS</dt><dd>{layer.qps ?? '不可比较'}</dd></div><div><dt>缓存命中率</dt><dd>{layer.cache_hit_ratio == null ? '不可比较' : `${(layer.cache_hit_ratio * 100).toFixed(1)}%`}</dd></div><div><dt>累计查询</dt><dd>{layer.counters?.['total.num.queries']?.toLocaleString() ?? '—'}</dd></div><div><dt>递归队列</dt><dd>{layer.counters?.['total.requestlist.current.all']?.toLocaleString() ?? '—'}</dd></div></dl>;
}
