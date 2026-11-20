import { useEffect, useState } from 'react';
import { diagnosticHash, parseDiagnosticRoute, queryRecordParams } from '../diagnosticRoute';
import { recentQueries } from '../queryRecords';
import type { QueryRecords, QuerySummary } from '../queryRecords';
import { errorText, when } from '../suiteApi';
import { useSuite } from '../suiteContext';
import { QueryDetails } from './RecentQueries';
import { Notice } from './SuiteUI';
import './BlockedQueries.css';

export default function BlockedQueries({ windowData }: { windowData?: Pick<QuerySummary, 'start' | 'end' | 'snapshot'> }) {
  const { caps, paused } = useSuite();
  const route = { ...parseDiagnosticRoute('#/diagnostics?tab=records&decision=block'),
    start: windowData?.start ?? null, end: windowData?.end ?? null, snapshot: windowData?.snapshot ?? null };
  const params = queryRecordParams(route);
  params.set('limit', windowData ? '5' : '25');
  const query = params.toString();
  const [result, setResult] = useState<{ query: string; records: QueryRecords } | null>(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState('');
  const [refresh, setRefresh] = useState(0);
  const fixed = !!windowData;
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
  if (!allowed) return null;
  return <section className="blocked-queries query-analytics-ranking" aria-label="最近拦截">
    <header><h3>最近拦截</h3><a href={diagnosticHash(route)}>全部拦截记录 →</a></header>
    {error && <Notice tone="error">{error} <button onClick={() => setRefresh(n => n + 1)}>重试</button></Notice>}
    {data?.history_changed && <p className="suite-note">所选时段的部分记录已清理。</p>}
    {data && !data.status.enabled && <p className="suite-note">查询记录已关闭。<a href="#/diagnostics?tab=records">管理查询记录</a></p>}
    {data && !data.events.length && <p className="suite-note">当前范围内没有已记录的拦截。</p>}
    {!error && !data && <p className="suite-note">正在读取拦截记录…</p>}
    <ul>{data?.events.map(event => <li key={event.id}>
      <button type="button" className="query-record-expand" aria-expanded={open === event.id}
        onClick={() => setOpen(open === event.id ? '' : event.id)}>{event.domain}</button>
      <div className="blocked-query-meta"><time>{when(event.observed_at)}</time>
        <a href={diagnosticHash({ ...route, source: event.source_ip, decision: '' })}>{event.source_ip}</a>
        <span>{event.filter?.source}</span></div>
      {open === event.id && <QueryDetails event={event} route={route} />}
    </li>)}</ul>
  </section>;
}
