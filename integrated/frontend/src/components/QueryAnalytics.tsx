import { Activity, ArrowUpRight, RefreshCw } from 'lucide-react';
import type { CSSProperties } from 'react';
import { useEffect, useState } from 'react';
import { diagnosticHash, parseDiagnosticRoute } from '../diagnosticRoute';
import type { DiagnosticRoute } from '../diagnosticRoute';
import { querySummary } from '../queryRecords';
import type { QueryRank, QuerySummary } from '../queryRecords';
import { errorText } from '../suiteApi';
import { navigationPosition, restoreQueryPosition, useQueryHistory } from '../queryHistory';
import { useSourceDevices } from '../sourceDevices';
import { SourceAddress } from './SourceDevices';
import { Empty, Notice, Section } from './SuiteUI';
import BlockedQueries from './BlockedQueries';
import './QueryAnalytics.css';

const clock = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
const localTime = (seconds: number) => clock.format(seconds * 1000);
const fullTime = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN');

export default function QueryAnalytics({ instance, paused }: { instance: string; paused: boolean }) {
  const history = useQueryHistory<{ hours: 1 | 24; summary: QuerySummary | null; expanded: string[] }>(
    'analytics', { hours: 1, summary: null, expanded: [] });
  const [hours, setHours] = useState(history.value.hours);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState(history.value.summary);
  const [retained, setRetained] = useState(!!history.value.summary);
  const [expanded, setExpanded] = useState(history.value.expanded);
  const [restore] = useState(history.entry);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(!data);

  useEffect(() => {
    if (restore) {
      const frame = requestAnimationFrame(() => restoreQueryPosition(restore));
      return () => cancelAnimationFrame(frame);
    }
  }, [restore]);

  useEffect(() => {
    if (retained) return;
    let controller: AbortController | undefined;
    let disposed = false;
    async function load() {
      if (controller) return;
      const request = new AbortController();
      controller = request;
      setLoading(true);
      try {
        const value = await querySummary(hours, request.signal);
        if (value.instance_id !== instance) throw new Error('查询统计的实例身份已变化');
        if (!disposed) {
          setData(value);
          setError('');
        }
      } catch (e) {
        if (!disposed) setError(errorText(e));
      } finally {
        controller = undefined;
        if (!disposed) setLoading(false);
      }
    }
    const first = window.setTimeout(() => void load(), 0);
    const timer = paused ? undefined : window.setInterval(() => {
      if (!document.hidden) void load();
    }, 30000);
    return () => {
      disposed = true;
      window.clearTimeout(first);
      window.clearInterval(timer);
      controller?.abort();
    };
  }, [hours, instance, paused, refresh, retained]);

  const result = data && data.end - data.start === hours * 3600 ? data : null;
  return <Section title="解析概览" subtitle={result ? fullTime(result.start) + ' – ' + fullTime(result.end) : undefined} icon={Activity} actions={<div className="query-analytics-actions">
    <select className="form-select" aria-label="统计时间范围" value={hours}
      onChange={event => {
        const next = event.target.value === '24' ? 24 : 1;
        setRetained(false); setHours(next); history.write({ hours: next, summary: null, expanded });
      }}>
      <option value="1">最近 1 小时</option><option value="24">最近 24 小时</option>
    </select>
    <button type="button" className="btn btn-secondary btn-sm" aria-label="刷新解析概览"
      onClick={() => { setRetained(false); setRefresh(value => value + 1); }} disabled={loading}><RefreshCw size={14} /></button>
  </div>}>
    {error && <Notice tone="error">{error}</Notice>}
    {result ? <div onClickCapture={event => {
      const position = navigationPosition(event);
      if (position) history.write({ hours, summary: result, expanded }, position);
    }}><AnalyticsContent data={result} expanded={expanded} toggle={title => {
      const next = expanded.includes(title) ? expanded.filter(value => value !== title) : [...expanded, title];
      setExpanded(next); history.write({ hours, summary: null, expanded: next });
    }} /></div> : !error && <Empty title="正在读取解析概览" />}
  </Section>;
}

function AnalyticsContent({ data, expanded, toggle }: { data: QuerySummary; expanded: string[]; toggle: (title: string) => void }) {
  const { devices } = useSourceDevices();
  const base = parseDiagnosticRoute('#/diagnostics?tab=records');
  const link = (patch: Partial<DiagnosticRoute> = {}) =>
    diagnosticHash({ ...base, start: data.start, end: data.end, snapshot: data.snapshot, ...patch });
  const peak = Math.max(1, ...data.buckets.map(bucket => bucket.count));
  const hasCoverage = data.buckets.some(bucket => bucket.coverage !== 'unknown');
  const knownBuckets = data.buckets.filter(bucket => bucket.coverage === 'complete').length;
  if (!data.status.enabled && !data.total) return <Notice>查询记录已关闭。<a href="#/diagnostics?tab=records&manage=1">管理采集</a></Notice>;
  return <>
    {!data.status.enabled && <Notice>查询记录已关闭。<a href="#/diagnostics?tab=records&manage=1">管理采集</a></Notice>}
    {data.status.enabled && !data.status.active && <Notice>正在等待入口采集恢复。{!hasCoverage && '当前时段覆盖未知。'}</Notice>}
    <div className="query-analytics-trend">
      <dl className="query-analytics-kpis">
        <div><dt>已记录查询</dt><dd><a href={link()} data-query-focus="total">{data.total.toLocaleString()}</a></dd></div>
        <div><dt>已确认拦截</dt><dd><a href={link({ decision: 'block' })} data-query-focus="blocked">{data.evaluated ? data.blocked.toLocaleString() : '—'}</a></dd>
          <small>{data.evaluated ? `${data.evaluated.toLocaleString()} 条有过滤判定 · ${(data.total - data.evaluated).toLocaleString()} 条无判定` : '尚无过滤判定样本'}</small></div>
        <div><dt>活跃来源 IP</dt><dd>{data.source_count.toLocaleString()}</dd></div>
        <div><dt>P95 响应耗时</dt><dd>{data.p95_ms === null ? '—' : data.p95_ms.toFixed(2) + ' ms'}</dd>
          <small>{data.latency_samples.toLocaleString()} 个响应样本
            {Object.keys(data.latency_scopes).length > 1 ? ' · 混合采集口径'
              : data.latency_scopes.mosdns_entry_processing ? ' · 核心处理' : data.latency_scopes.passive_entry_packet_interval ? ' · 入口报文间隔' : ''}
          </small></div>
      </dl>
      <div className="query-analytics-chart-wrap">
        <div className="query-analytics-chart-heading"><h3>查询量</h3><a className="analytics-log-link" href={link()} data-query-focus="analytics-records">查询记录<ArrowUpRight size={14} /></a></div>
        <div className="query-analytics-chart" aria-label="查询量时间图">
          {data.buckets.map((bucket, index) => {
            const coverage = bucket.coverage === 'complete' ? '覆盖完整' : bucket.coverage === 'partial' ? '部分覆盖' : '覆盖未知';
            const label = localTime(bucket.start) + ' – ' + localTime(bucket.end) + '，' + bucket.count + ' 条，' + coverage;
            const height = bucket.count ? Math.max(4, 100 * bucket.count / peak) + '%' : undefined;
            return <a key={bucket.start} href={link({ start: bucket.start, end: bucket.end })}
              aria-label={label} title={label} data-query-focus={`bucket:${bucket.start}`} className={'query-analytics-bucket ' + bucket.coverage}>
              <span className="query-analytics-bar" style={{ height }} data-count={bucket.count} />
              {index % Math.ceil(data.buckets.length / 6) === 0 && <small>{localTime(bucket.start)}</small>}
            </a>;
          })}
        </div>
        <p className="query-analytics-key">
          <span><i className="complete" /> 覆盖完整</span><span><i className="partial" /> 部分覆盖</span>
          <span><i className="unknown" /> 覆盖未知</span>
          {knownBuckets < data.buckets.length && <span>缺口时段仅统计保留的记录</span>}
        </p>
      </div>
    </div>
    <div className="query-analytics-ranks">
      <Ranking title="来源 IP 排行" items={data.sources} devices={devices} expanded={expanded} toggle={toggle} href={value => link({ source: value })} />
      <Ranking title="热门域名" items={data.domains} expanded={expanded} toggle={toggle} href={value => link({ exactDomain: value })} />
      {!!data.blocked_sources.length && <Ranking title="拦截来源排行" items={data.blocked_sources}
        devices={devices} expanded={expanded} toggle={toggle} href={value => link({ source: value, decision: 'block' })} />}
    </div>
    <BlockedQueries key={data.end - data.start} windowData={data} />
  </>;
}

function Ranking({ title, items, href, devices, expanded, toggle }: { title: string; items: QueryRank[]; href: (value: string) => string;
  devices?: Record<string, string>; expanded: string[]; toggle: (title: string) => void }) {
  const open = expanded.includes(title);
  const largest = Math.max(1, ...items.map(item => item.count));
  const visible = open ? items : items.slice(0, 5);
  return <div className="query-analytics-ranking"><h3>{title}</h3>
    {items.length ? <ul>{visible.map(item => <li key={item.value}>
      <a className="analytics-rank-link" href={href(item.value)} data-query-focus={`${title}:${item.value}`}
        style={{ '--rank-width': (item.count / largest * 100) + '%' } as CSSProperties}>
        {devices ? <SourceAddress ip={item.value} name={devices[item.value]} /> : <code>{item.value}</code>}
        <span>{item.count.toLocaleString()}</span></a>
    </li>)}</ul> : <p className="suite-note">暂无记录</p>}
    {items.length > 5 && <button type="button" className="analytics-rank-more" aria-expanded={open}
      onClick={() => toggle(title)}>{open ? '收起排行' : `查看其余 ${items.length - 5} 项`}</button>}
  </div>;
}
