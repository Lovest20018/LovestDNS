import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import { recentQueries, recordingValid } from '../queryRecords';
import type { QueryEvent, QueryRecords, QueryRecording } from '../queryRecords';
import { diagnosticHash, queryRecordParams } from '../diagnosticRoute';
import type { DiagnosticRoute } from '../diagnosticRoute';
import { RECORD_TYPES } from '../reports';
import { Empty, Notice, Section } from './SuiteUI';
import { dnsAddress } from '../dnsListeners';
import { applyFilter, filterLabels, filterState } from '../filterApi';
import { useFilterView } from '../filterContext';
import './RecentQueries.css';

const PHASE = { disabled: '未开启查询记录', starting: '正在启动记录', recording: '后台记录已开启', unavailable: '后台记录暂不可用' };
const DROP_KEYS = ['kernel_drops', 'queue_drops', 'storage_drops', 'pending_evictions', 'stream_evictions', 'tcp_gaps', 'discarded_pending', 'core_event_drops'];
const COUNTERS: Record<string, string> = { queries_observed: '入口事务', stored_events: '已保存', kernel_drops: '内核丢包', queue_drops: '队列丢弃', storage_drops: '保存丢弃', storage_errors: '存储错误', pending_evictions: '待响应容量丢弃', stream_evictions: 'TCP 容量丢弃', tcp_gaps: 'TCP 缺口', discarded_pending: '关联重置丢弃', discarded_events: '启停或清空丢弃', unparsed_packets: '未解析报文', tcp_untracked_packets: '未跟踪 TCP 报文', unmatched_responses: '未配对响应', no_response_observed: '未观察到响应', same_key_queries: '同键请求', invalid_timing: '无效计时', clock_resets: '时钟跳变重置', tcp_retransmitted_packets: 'TCP 重传报文', tcp_idle_expirations: 'TCP 跟踪到期', capture_errors: '采集错误' };
type Navigate = (patch: Partial<DiagnosticRoute>, replace?: boolean) => void;
const FILTER_TYPES = [['1', 'A'], ['28', 'AAAA'], ['5', 'CNAME'], ['16', 'TXT'], ['64', 'SVCB'], ['65', 'HTTPS']];
const responseText = (event: QueryEvent) => event.outcome === 'no_response_observed' ? '未观察到响应' : event.rcode ?? '未知响应';
const addressText = (event: QueryEvent) => event.addresses.length ? event.addresses.join(' · ') + (event.addresses_complete ? '' : '（部分）') : event.answer_count === 0 ? '无 Answer 记录' : '无可展示 A / AAAA 地址';
const elapsedText = (event: QueryEvent) => event.elapsed_ms === null ? '—' : `${event.elapsed_ms} ms`;
const date = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN');
const RESPONSES = [['NOERROR', 'NOERROR'], ['NXDOMAIN', 'NXDOMAIN'], ['SERVFAIL', 'SERVFAIL'],
  ['REFUSED', 'REFUSED'], ['FORMERR', 'FORMERR'], ['NOTIMP', 'NOTIMP'], ['no_response_observed', '未观察到响应']];

export default function RecentQueries({ route, navigate }: { route: DiagnosticRoute; navigate: Navigate }) {
  const { caps, paused } = useSuite();
  const instanceId = caps?.instance_id ?? '';
  const params = queryRecordParams(route).toString();
  const dataKey = `${instanceId}:${params}`;
  const [stored, setStored] = useState<{ key: string; value: QueryRecords } | null>(null);
  const data = stored?.key === dataKey ? stored.value : null;
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const allowed = caps?.features.query_recording === true && caps.permissions.configure;
  const load = useCallback(async () => {
    if (!allowed) return;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setBusy(true);
    try {
      const result = await recentQueries(new URLSearchParams(params), controller.signal);
      if (controller.signal.aborted) return;
      if (result.instance_id !== instanceId) throw new Error('查询记录的实例身份已变化，请刷新服务信息');
      if (new URLSearchParams(params).has('before') && !result.events.length) {
        setNotice('此较早页已无符合条件的记录，可能已按保留规则清理；已返回最近页。');
        navigate({ before: null }, true);
        return;
      }
      setStored({ key: dataKey, value: result }); setError('');
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }, [allowed, params, instanceId, dataKey, navigate]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => { window.clearTimeout(timer); request.current?.abort(); }; }, [load]);
  useEffect(() => {
    if (!route.automatic || paused || route.before !== null || route.end !== null) return;
    const timer = window.setInterval(() => { if (!document.hidden && !busy) void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [route.automatic, paused, route.before, route.end, busy, load]);
  const change = async (clear = false) => {
    if (!data || busy) return;
    setBusy(true); setError(''); request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    try {
      const status = await suiteFetch<QueryRecording>(clear ? '/api/v1/query-records' : '/api/v1/query-recording', {
        method: clear ? 'DELETE' : 'PUT', signal: controller.signal,
        ...(clear ? {} : { body: JSON.stringify({ enabled: !data.status.enabled }) }),
      });
      if (!recordingValid(status)) throw new Error('记录状态响应格式无效');
      if (!controller.signal.aborted) {
        setStored(old => old ? { ...old, value: { ...old.value, status, ...(clear ? {
          events: [], next_before: null, history_changed: route.snapshot !== null,
        } : {}) } } : old);
        setConfirmClear(false);
        if (clear) navigate({ before: null }, true);
      }
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  if (!allowed) return null;
  const status = data?.status;
  const losses = status ? DROP_KEYS.reduce((total, key) => total + (status.counters[key] ?? 0), 0) : 0;
  const sourceLink = (event: QueryEvent) => <a aria-label={`筛选来源 ${event.source_ip}`}
    href={diagnosticHash({ ...route, source: event.source_ip, before: null })}><code>{event.source_ip}</code></a>;
  const open = (event: QueryEvent) => expanded === dataKey + event.id;
  const toggle = (event: QueryEvent) => setExpanded(open(event) ? null : dataKey + event.id);
  return <Section title="最近查询" subtitle="当前实例已记录的入口查询" icon={Database}
    actions={<button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void load()}><RefreshCw size={14} />{busy ? '读取中…' : '刷新记录'}</button>}>
    {error && <Notice tone="error">{error}{data && '；下方保留上次结果。'}</Notice>}
    {notice && <Notice>{notice}</Notice>}
    {data?.history_changed && <Notice tone="warning">此统计窗口的部分记录可能已过期、清空或达到容量上限。<a href="#/">返回解析概览</a></Notice>}
    {status && <><div className="query-recording-status"><div><strong>{PHASE[status.phase]}</strong><span>{status.retained_rows.toLocaleString()} 条 · 数据库 {(status.database_bytes / 1048576).toFixed(1)} MiB</span></div><div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void change()}>{status.enabled ? '关闭记录' : '开启记录'}</button><button className="btn btn-secondary btn-sm" disabled={busy || !status.retained_rows} onClick={() => setConfirmClear(true)}>清空记录</button></div></div>
      {!status.enabled && <Notice>开启后会保存来源 IP 和查询域名。关闭记录会保留已有记录，仍按保留期限清理。</Notice>}
      {status.phase === 'unavailable' && <Notice tone="warning">当前 DNS 入口的记录不可用。请检查 DNS 入口、采集服务及权限。</Notice>}
      {confirmClear && <Notice tone="warning"><p>清空全部查询记录及等待中的采集事件？</p><div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void change(true)}>确认清空记录</button><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setConfirmClear(false)}>取消</button></div></Notice>}
    </>}
    <QueryFilters key={JSON.stringify([route.source, route.filterDomain, route.qtype, route.response])} route={route} busy={busy} onApply={patch => {
      setNotice('');
      const next = { ...route, ...patch, before: null };
      if (queryRecordParams(next).toString() === params) void load();
      else navigate({ ...patch, before: null });
    }} />
    <div className="query-record-toolbar">
      <label>时间范围 <select className="form-select" aria-label="查询时间范围" value={route.end === null ? 'recent' : 'fixed'}
        onChange={e => {
          if (e.target.value === 'recent') navigate({ start: null, end: null, snapshot: null, before: null });
          else {
            const end = Math.ceil(Date.now() / 1000);
            navigate({ start: end - Number(e.target.value) * 3600, end, snapshot: null, before: null });
          }
        }}>
        <option value="recent">全部保留记录</option><option value="1">最近 1 小时</option><option value="24">最近 24 小时</option>
        {route.end !== null && <option value="fixed">已选时间段</option>}
      </select></label>
      {route.start !== null && route.end !== null && <span>{date(route.start)} – {date(route.end)} · <a href="#/">解析概览</a></span>}
      {route.exactDomain && <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate({ exactDomain: '', before: null })}>精确域名 {route.exactDomain} ×</button>}
      {route.decision && <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate({ decision: '', before: null })}>过滤判定 {route.decision === 'block' ? '拦截' : route.decision === 'allow' ? '放行' : '继续解析'} ×</button>}
      {route.end === null && <label><input type="checkbox" checked={route.automatic} onChange={e => navigate({ automatic: e.target.checked })} /> 每 5 秒刷新最近页{paused ? '（已暂停）' : ''}</label>}
      {status && losses > 0 && <span>{losses} 项丢弃或采集缺口</span>}
    </div>
    {data?.events.length ? <>
      <div className="suite-table-scroll query-record-desktop">
        <table className="info-table query-record-table">
          <thead><tr><th>时间 / 来源 IP</th><th>域名 / 类型</th><th>响应</th><th>观测耗时</th></tr></thead>
          <tbody>{data.events.map(event => <Fragment key={event.id}>
            <tr><td><time>{date(event.observed_at)}</time>{sourceLink(event)}</td>
              <td><button type="button" className="query-record-expand" aria-expanded={open(event)} onClick={() => toggle(event)}>{event.domain}</button>
                <span>{event.record_type} · {event.transport.toUpperCase()}</span></td>
              <td><strong>{responseText(event)}{event.truncated ? ' · 报文截断' : ''}</strong>
                {event.filter?.action === 'block' && <span className="query-record-blocked">DNS 过滤拦截</span>}</td>
              <td>{elapsedText(event)}</td></tr>
            {open(event) && <tr className="query-record-detail-row"><td colSpan={4}><QueryDetails event={event} route={route} /></td></tr>}
          </Fragment>)}</tbody>
        </table>
      </div>
      <ul className="query-record-cards" aria-label="查询记录">{data.events.map(event => <li key={event.id} className="query-record-card">
        <div className="query-record-card-source">{sourceLink(event)}<time>{date(event.observed_at)}</time></div>
        <button type="button" className="query-record-expand" aria-expanded={open(event)} onClick={() => toggle(event)}>{event.domain}</button>
        <span className="query-record-card-type">{event.record_type} · {event.transport.toUpperCase()}</span>
        <dl className="query-record-card-result"><div><dt>响应</dt><dd>{responseText(event)}{event.truncated ? ' · 报文截断' : ''}
          {event.filter?.action === 'block' && <span className="query-record-blocked">DNS 过滤拦截</span>}</dd></div><div><dt>观测耗时</dt><dd>{elapsedText(event)}</dd></div></dl>
        {open(event) && <QueryDetails event={event} route={route} />}
      </li>)}</ul>
    </> : <Empty title={busy ? '正在读取查询记录' : '暂无符合条件的记录'}>{status?.enabled ? '确认客户端请求已到达当前入口，或调整筛选条件。' : '开启记录后，新的入口请求会显示在这里。'}</Empty>}
    {data && <div className="suite-button-row query-record-pages"><button className="btn btn-secondary btn-sm" disabled={busy || route.before === null} onClick={() => navigate({ before: null })}>返回最近</button><button className="btn btn-secondary btn-sm" disabled={busy || data.next_before === null} onClick={() => navigate({ before: data.next_before })}>查看较早记录</button></div>}
    <details className="suite-editor-help"><summary>采集范围与保留规则</summary>
      <p className="suite-note">来源为 DNS 入口看到的 IP，路由器代转时可能只显示路由器。UDP 与 TCP 查询分别计数。MosDNS 事件记录核心处理时间，报文采集记录请求与应答之间的间隔。</p>
      <p className="suite-note">最多保留 24 小时及 100,000 条，数据库上限 31 MiB，总存储预算 64 MiB。每条最多展示 4 个 A / AAAA 地址。</p>
      {status && <p className="suite-note">本次采集计数 {Object.entries(status.counters).map(([key, value]) => `${COUNTERS[key] ?? key} ${value}`).join(' · ') || '尚无事件'}</p>}
    </details>
  </Section>;
}

export function QueryDetails({ event, route }: { event: QueryEvent; route: DiagnosticRoute }) {
  const { caps } = useSuite();
  const { publish } = useFilterView();
  const verdict = event.filter;
  const [notice, setNotice] = useState('');
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const allow = async (mode: 'temporary' | 'permanent') => {
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError(false);
    setNotice('');
    try {
      const current = await filterState(controller.signal);
      if (!current.available) throw new Error('当前解析入口无法应用放行');
      const result = await applyFilter({ action: 'allow', domain: event.domain, mode }, current.revision, controller.signal);
      if (controller.signal.aborted) return;
      publish(result.state);
      if (result.job.phase === 'failed') throw new Error(result.job.message);
      const name = event.domain.toLowerCase().replace(/\.$/, '');
      const until = result.state.allow[name];
      setNotice(until === null ? '永久放行已生效' : until ? '放行已生效，到期时间 ' + date(until) : result.job.message);
    } catch (e) {
      if (!controller.signal.aborted) { setNotice(errorText(e)); setError(true); }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return <div className="query-record-detail">
    <dl>
      <div><dt>查询</dt><dd>{event.domain} · {event.record_type} · {event.transport.toUpperCase()}</dd></div>
      <div><dt>来源</dt><dd><code>{dnsAddress(event.source_ip, event.source_port)}</code></dd></div>
      <div><dt>入口</dt><dd><code>{dnsAddress(event.entry_address, event.entry_port)}</code> · {event.component}</dd></div>
      <div><dt>应答</dt><dd>{responseText(event)} · {addressText(event)}</dd></div>
      <div><dt>应答时间</dt><dd>{event.response_at === null ? '—' : date(event.response_at)} · {elapsedText(event)}</dd></div>
      <div><dt>耗时口径</dt><dd>{event.timing_scope === 'mosdns_entry_processing' ? 'MosDNS 核心处理' : '入口报文间隔'}</dd></div>
      <div><dt>过滤判定</dt><dd>{verdict ? filterLabels[verdict.action] : '未取得判定'}</dd></div>
      {verdict && <div><dt>活动版本</dt><dd><code>{verdict.revision}</code></dd></div>}
      {verdict?.rule && <div><dt>命中规则</dt><dd><code>{verdict.rule}</code> · {verdict.source}</dd></div>}
      {event.same_key_packets > 1 && <div><dt>同键请求</dt><dd>{event.same_key_packets} 次</dd></div>}
    </dl>
    {notice && <Notice tone={error ? 'error' : undefined}>{notice}</Notice>}
    <div className="suite-button-row">
    {verdict?.action === 'block' && caps?.features.dns_filter && <>
      <span className="suite-note">精确放行作用于整个实例。</span>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void allow('temporary')}>精确放行 10 分钟</button>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void allow('permanent')}>永久放行此域名</button>
    </>}
    {RECORD_TYPES.some(type => type === event.record_type) &&
      <a className="btn btn-secondary btn-sm query-record-query"
        href={diagnosticHash({ ...route, tab: 'query', domain: event.domain, record: event.record_type })}
        aria-label={`主动查询 ${event.domain} ${event.record_type}`}>重新查询</a>}
    </div>
  </div>;
}

function QueryFilters({ route, busy, onApply }: { route: DiagnosticRoute; busy: boolean; onApply: (patch: Partial<DiagnosticRoute>) => void }) {
  const [source, setSource] = useState(route.source);
  const [domain, setDomain] = useState(route.filterDomain);
  const [qtype, setQtype] = useState(route.qtype);
  const [response, setResponse] = useState(route.response);
  return <form className="query-record-filters" onSubmit={e => { e.preventDefault(); onApply({ source: source.trim(), filterDomain: domain.trim(), qtype, response }); }}>
    <label>来源 IP<input className="form-input" aria-label="筛选来源 IP" maxLength={45} value={source} onChange={e => setSource(e.target.value)} placeholder="完整 IPv4 / IPv6" /></label>
    <label>域名包含<input className="form-input" maxLength={253} aria-label="筛选查询域名" value={domain} onChange={e => setDomain(e.target.value)} placeholder="example.com" /></label>
    <label>类型<select className="form-select" aria-label="筛选查询类型" value={qtype} onChange={e => setQtype(e.target.value)}><option value="">全部</option>{FILTER_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}{qtype && !FILTER_TYPES.some(([value]) => value === qtype) && <option value={qtype}>类型 {qtype}</option>}</select></label>
    <label>响应<select className="form-select" aria-label="筛选响应" value={response} onChange={e => setResponse(e.target.value)}><option value="">全部</option>
      {RESPONSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      {response && !RESPONSES.some(([value]) => value === response) && <option value={response}>{response}</option>}
    </select></label>
    <div className="suite-button-row"><button className="btn btn-secondary" disabled={busy}>筛选</button>
      <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => {
        setSource(''); setDomain(''); setQtype(''); setResponse('');
        onApply({ source: '', filterDomain: '', exactDomain: '', qtype: '', response: '', decision: '' });
      }}>清除筛选</button>
    </div>
  </form>;
}
