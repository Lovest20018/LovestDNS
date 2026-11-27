import { useEffect, useRef, useState } from 'react';
import { diagnosticHash, parseDiagnosticRoute, queryRecordParams } from '../diagnosticRoute';
import { recentQueries } from '../queryRecords';
import type { QueryRecords, QuerySummary } from '../queryRecords';
import { errorText, when } from '../suiteApi';
import { useSuiteConfig } from '../suiteContext';
import { navigationPosition, restoreQueryPosition, useQueryHistory } from '../queryHistory';
import { useSourceDevices } from '../sourceDevices';
import { QueryDetails } from './RecentQueries';
import { SourceAddress } from './SourceDevices';
import { Notice } from './SuiteUI';
import './BlockedQueries.css';

type Range = Pick<QuerySummary, 'start' | 'end' | 'snapshot'>;
type Pages = { window?: Range; snapshot?: number; cursors: number[] };

export default function BlockedQueries({ windowData }: { windowData?: Pick<QuerySummary, 'start' | 'end' | 'snapshot'> }) {
  const { caps, paused } = useSuiteConfig();
  const { devices } = useSourceDevices();
  const memory = useQueryHistory<{ pages: Pages | null; open: string; snapshot?: number; live?: boolean }>(
    windowData ? 'homeBlocks' : 'filterBlocks', { pages: null, open: '' });
  const { pages, open } = memory.value;
  const range = memory.value.live ? undefined : pages?.window ?? windowData;
  const route = { ...parseDiagnosticRoute('#/diagnostics?tab=records&decision=block'),
    start: range?.start ?? null, end: range?.end ?? null, snapshot: range?.snapshot ?? pages?.snapshot ?? memory.value.snapshot ?? null };
  const params = queryRecordParams(route);
  params.set('limit', pages ? '25' : '5');
  const before = pages?.cursors.at(-1);
  if (before) params.set('before', String(before));
  const query = params.toString();
  const [result, setResult] = useState<{ query: string; records: QueryRecords } | null>(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const fixed = !!range || !!pages || route.snapshot !== null;
  const instance = caps?.instance_id;
  const allowed = !!(caps?.permissions.configure && caps.features.query_recording);
  useEffect(() => {
    if (!allowed) return;
    let controller: AbortController | undefined;
    async function load() {
      if (controller) return;
      const request = new AbortController();
      controller = request;
      try {
        const records = await recentQueries(new URLSearchParams(query), request.signal);
        if (records.instance_id !== instance) throw new Error('查询记录的实例已变化');
        if (!request.signal.aborted) { setResult({ query, records }); setError(''); }
      } catch (e) {
        if (!request.signal.aborted) setError(errorText(e));
      } finally { controller = undefined; }
    }
    void load();
    const timer = !fixed && !paused ? window.setInterval(() => { if (!document.hidden) void load(); }, 30000) : undefined;
    return () => { window.clearInterval(timer); controller?.abort(); };
  }, [allowed, fixed, paused, instance, query, refresh]);
  const data = result?.query === query ? result.records : null;
  const restored = useRef<typeof memory.restoration>(null);
  const expandButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!data || !memory.restoration || restored.current === memory.restoration) return;
    const item = memory.restoration;
    const frame = requestAnimationFrame(() => {
      restored.current = item;
      const missing = item.payload.open && !data.events.some(event => event.id === item.payload.open);
      restoreQueryPosition(missing ? { focus: 'blocks-recent' } : item);
    });
    return () => cancelAnimationFrame(frame);
  }, [data, memory.restoration]);
  const missing = !!(data && open && !data.events.some(event => event.id === open)) || !!(data && before && !data.events.length);
  const recent = (live = memory.value.live) => {
    memory.write({ pages: null, open: '', live });
    memory.dismissRestoration();
  };
  if (!allowed) return null;
  return <section className="blocked-queries" aria-label="最近拦截" onClickCapture={event => {
    const position = navigationPosition(event);
    if (position) memory.write({ ...memory.value, snapshot: data?.snapshot }, { ...position, focus: open ? `block:${open}` : position.focus });
  }}>
    <header><h3>{pages ? '拦截记录' : '最近拦截'}</h3>
      <button type="button" ref={expandButton} className="btn btn-secondary btn-sm" aria-expanded={!!pages} onClick={() => {
        if (pages) recent();
        else memory.write({ ...memory.value, pages: { window: range, snapshot: data?.snapshot, cursors: [] }, open: '' });
      }}>{pages ? '收起记录' : '全部拦截记录'}</button></header>
    {error && <Notice tone="error">{error} <button onClick={() => setRefresh(n => n + 1)}>重试</button></Notice>}
    {missing && <Notice tone="warning">原拦截记录可能已按保留规则清理。 <button type="button" className="btn btn-secondary btn-sm"
      data-query-focus="blocks-recent" onClick={() => { recent(true); expandButton.current?.focus(); }}>返回最近拦截</button></Notice>}
    {data?.history_changed && <p className="suite-note">所选时段的部分记录已清理。</p>}
    {data && !data.status.enabled && <p className="suite-note">查询记录已关闭。<a href="#/diagnostics?tab=records&manage=1">管理采集</a></p>}
    {data && !data.events.length && <p className="suite-note">当前范围内没有已记录的拦截。</p>}
    {!error && !data && <p className="suite-note">正在读取拦截记录…</p>}
    {!!data?.events.length && <div className="blocked-query-columns" aria-hidden="true"><span>域名</span><span>设备来源</span><span>命中来源</span><span>时间</span></div>}
    <ul>{data?.events.map(event => <li key={event.id}>
      <div className="blocked-query-summary">
        <button type="button" className="query-record-expand" aria-expanded={open === event.id} data-query-focus={`block:${event.id}`}
          onClick={() => memory.write({ ...memory.value, open: open === event.id ? '' : event.id })}>{event.domain}</button>
        <a href={diagnosticHash({ ...route, source: event.source_ip, decision: '' })} data-query-focus={`block-source:${event.id}`}>
          <SourceAddress ip={event.source_ip} name={devices[event.source_ip]} /></a>
        <span>{event.filter?.source || '—'}</span><time>{when(event.observed_at)}</time></div>
      {open === event.id && <QueryDetails event={event} route={route} />}
    </li>)}</ul>
    {pages && <footer className="blocked-query-pages">
      <span>第 {pages.cursors.length + 1} 页</span>
      <button className="btn btn-secondary btn-sm" disabled={!pages.cursors.length} onClick={() => {
        memory.write({ ...memory.value, pages: { ...pages, cursors: pages.cursors.slice(0, -1) }, open: '' });
      }}>上一页</button>
      <button className="btn btn-secondary btn-sm" disabled={!data?.next_before} onClick={() => {
        if (data?.next_before) memory.write({ ...memory.value, pages: { ...pages, cursors: [...pages.cursors, data.next_before] }, open: '' });
      }}>下一页</button>
    </footer>}
  </section>;
}
