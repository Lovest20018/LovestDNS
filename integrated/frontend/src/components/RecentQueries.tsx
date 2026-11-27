import { Fragment, memo, useEffect, useRef, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import { useSuiteConfig } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import { recentQueries, recordingValid } from '../queryRecords';
import type { QueryEvent, QueryRecords, QueryRecording } from '../queryRecords';
import { diagnosticHash, queryRecordParams } from '../diagnosticRoute';
import type { DiagnosticRoute } from '../diagnosticRoute';
import { RECORD_TYPES } from '../reports';
import { Empty, Notice, Section } from './SuiteUI';
import { dnsAddress } from '../dnsListeners';
import { filterLabels } from '../filterApi';
import { useFilterView } from '../filterContext';
import { navigationPosition, restoreQueryPosition, useQueryHistory } from '../queryHistory';
import { useSourceDevices } from '../sourceDevices';
import { useMediaQuery } from '../useMediaQuery';
import { SourceAddress, SourceDeviceEditor } from './SourceDevices';
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

function RecentQueries({ route: inputRoute, navigate }: { route: DiagnosticRoute; navigate: Navigate }) {
  const { caps, paused } = useSuiteConfig();
  const compact = useMediaQuery('(max-width: 700px)');
  const { devices } = useSourceDevices();
  const memory = useQueryHistory<{ expanded: string; snapshot?: number }>('queryRecords', { expanded: '' });
  const route = { ...inputRoute, snapshot: inputRoute.snapshot ?? memory.restoration?.payload.snapshot ?? null };
  const instanceId = caps?.instance_id ?? '';
  const params = queryRecordParams(route).toString();
  const dataKey = `${instanceId}:${params}`;
  const [stored, setStored] = useState<{ key: string; value: QueryRecords } | null>(null);
  const data = stored?.key === dataKey ? stored.value : null;
  const hasData = !!data;
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [confirmClear, setConfirmClear] = useState(false);
  const [deviceDraft, setDeviceDraft] = useState<{ dataKey: string; eventId: string; name: string } | null>(null);
  const expanded = memory.value.expanded;
  const request = useRef<AbortController | null>(null);
  const allowed = caps?.features.query_recording === true && caps.permissions.configure;
  const reload = () => setRefresh(value => value + 1);
  useEffect(() => {
    if (!allowed) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      request.current?.abort();
      request.current = controller;
      setBusy(true);
      try {
        const result = await recentQueries(new URLSearchParams(params), controller.signal);
        if (controller.signal.aborted) return;
        if (result.instance_id !== instanceId) throw new Error('查询记录的实例身份已变化，请刷新服务信息');
        setStored({ key: dataKey, value: result }); setError('');
      } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
      finally { if (!controller.signal.aborted) setBusy(false); }
    }, 0);
    return () => {
      window.clearTimeout(timer);
      request.current?.abort();
    };
  }, [allowed, params, instanceId, dataKey, refresh]);
  useEffect(() => {
    if (!route.automatic || paused || route.before !== null || route.end !== null || route.snapshot !== null) return;
    const timer = window.setInterval(() => { if (!document.hidden && !busy) setRefresh(value => value + 1); }, 5000);
    return () => window.clearInterval(timer);
  }, [route.automatic, paused, route.before, route.end, route.snapshot, busy]);
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
        if (clear) {
          setDeviceDraft(null);
          memory.write({ expanded: '' });
          memory.dismissRestoration();
          navigate({ before: null, snapshot: route.end === null ? null : route.snapshot }, true);
        }
      }
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  const manage = useRef<HTMLDetailsElement>(null);
  const results = useRef<HTMLDivElement>(null);
  const showManagement = () => {
    const panel = manage.current;
    if (!panel) return;
    panel.open = true;
    const summary = panel.querySelector('summary');
    summary?.focus({ preventScroll: true });
    summary?.scrollIntoView({ block: 'center' });
  };
  useEffect(() => {
    if (!inputRoute.manage) return;
    const frame = requestAnimationFrame(showManagement);
    return () => cancelAnimationFrame(frame);
  }, [inputRoute.manage, hasData]);
  const restored = useRef<typeof memory.restoration>(null);
  useEffect(() => {
    if (!data || !memory.restoration?.focus || restored.current === memory.restoration) return;
    const item = memory.restoration;
    const frame = requestAnimationFrame(() => {
      restored.current = item;
      const missing = item.payload.expanded && !data.events.some(event => event.id === item.payload.expanded);
      restoreQueryPosition(missing ? { focus: 'records-recent' } : item);
    });
    return () => cancelAnimationFrame(frame);
  }, [data, memory.restoration]);
  if (!allowed) return null;
  const status = data?.status;
  const losses = status ? DROP_KEYS.reduce((total, key) => total + (status.counters[key] ?? 0), 0) : 0;
  const sourceLink = (event: QueryEvent) => <a aria-label={`筛选来源 ${event.source_ip}`}
    data-query-focus={`record-source:${event.id}`} href={diagnosticHash({ ...route, source: event.source_ip, before: null })}>
    <SourceAddress ip={event.source_ip} name={devices[event.source_ip]} /></a>;
  const open = (event: QueryEvent) => expanded === event.id;
  const toggle = (event: QueryEvent) => {
    setDeviceDraft(null);
    memory.write({ ...memory.value, expanded: open(event) ? '' : event.id });
  };
  // Keep an open device note draft when the expanded row changes layout.
  const details = (event: QueryEvent) => <QueryDetails event={event} route={route} deviceEditor={{
    draft: deviceDraft?.dataKey === dataKey && deviceDraft.eventId === event.id ? deviceDraft.name : null,
    setDraft: name => setDeviceDraft(current => name !== null ? { dataKey, eventId: event.id, name }
      : current?.dataKey === dataKey && current.eventId === event.id ? null : current),
  }} />;
  const recent = (discardWindow = false) => {
    setDeviceDraft(null);
    memory.write({ expanded: '' });
    memory.dismissRestoration();
    navigate({
      before: null,
      snapshot: discardWindow || route.end === null ? null : route.snapshot,
      ...(discardWindow ? { start: null, end: null } : {}),
    });
    if (discardWindow) results.current?.focus();
  };
  const missing = !!(data && expanded && !data.events.some(open)) || !!(data && route.before !== null && !data.events.length);
  return <Section title="查询记录" subtitle="当前实例已记录的入口查询" icon={Database}
    actions={<div className="suite-button-row">
      <button type="button" className="btn btn-secondary btn-sm" onClick={showManagement}>管理采集</button>
      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={reload}><RefreshCw size={14} />{busy ? '读取中…' : '刷新记录'}</button>
    </div>}>
    {error && <Notice tone="error">{error}{data && '；下方保留上次结果。'}</Notice>}
    {data?.history_changed && <Notice tone="warning">此统计窗口的部分记录可能已过期、清空或达到容量上限。<a href="#/">返回解析概览</a></Notice>}
    {missing && <Notice tone="warning">原查询记录可能已按保留规则清理。
      <button type="button" className="btn btn-secondary btn-sm" data-query-focus="records-recent" onClick={() => recent(true)}>返回最近记录</button>
    </Notice>}
    <QueryFilters route={route} busy={busy} onApply={patch => {
      const next = { ...route, ...patch, before: null };
      if (queryRecordParams(next).toString() === params) reload();
      else navigate({ ...patch, before: null, snapshot: route.end === null ? null : route.snapshot });
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
    <div ref={results} tabIndex={-1} aria-label="查询结果" onClickCapture={event => {
      const position = navigationPosition(event);
      if (position) memory.write({ expanded, snapshot: data?.snapshot }, { ...position, focus: expanded ? `record:${expanded}` : position.focus });
    }}>
    {!data?.events.length ? <Empty title={busy ? '正在读取查询记录' : '暂无符合条件的记录'}>{status?.enabled ? '确认客户端请求已到达当前入口，或调整筛选条件。' : '开启记录后，新的入口请求会显示在这里。'}</Empty>
      : !compact ? <div className="suite-table-scroll query-record-desktop">
        <table className="info-table query-record-table">
          <thead><tr><th>查询域名</th><th>设备来源 / 时间</th><th>响应</th><th>观测耗时</th></tr></thead>
          <tbody>{data.events.map(event => <Fragment key={event.id}>
            <tr><td><button type="button" className="query-record-expand" data-query-focus={`record:${event.id}`} aria-expanded={open(event)} onClick={() => toggle(event)}>{event.domain}</button>
                <span>{event.record_type} · {event.transport.toUpperCase()}</span></td>
              <td>{sourceLink(event)}<time>{date(event.observed_at)}</time></td>
              <td><strong>{responseText(event)}{event.truncated ? ' · 报文截断' : ''}</strong>
                {event.filter?.action === 'block' && <span className="query-record-blocked">DNS 过滤拦截</span>}</td>
              <td>{elapsedText(event)}</td></tr>
            {open(event) && <tr className="query-record-detail-row"><td colSpan={4}>{details(event)}</td></tr>}
          </Fragment>)}</tbody>
        </table>
      </div> : <ul className="query-record-cards" aria-label="查询记录">{data.events.map(event => <li key={event.id} className="query-record-card">
        <button type="button" className="query-record-expand" data-query-focus={`record:${event.id}`} aria-expanded={open(event)} onClick={() => toggle(event)}>{event.domain}</button>
        <span className="query-record-card-type">{event.record_type} · {event.transport.toUpperCase()}</span>
        <div className="query-record-card-source">{sourceLink(event)}<time>{date(event.observed_at)}</time></div>
        <dl className="query-record-card-result"><div><dt>响应</dt><dd>{responseText(event)}{event.truncated ? ' · 报文截断' : ''}
          {event.filter?.action === 'block' && <span className="query-record-blocked">DNS 过滤拦截</span>}</dd></div><div><dt>观测耗时</dt><dd>{elapsedText(event)}</dd></div></dl>
        {open(event) && details(event)}
      </li>)}</ul>}
    </div>
    {data && <div className="suite-button-row query-record-pages"><button className="btn btn-secondary btn-sm" disabled={busy || route.before === null && (route.end !== null || route.snapshot === null)} onClick={() => recent()}>返回最近</button><button className="btn btn-secondary btn-sm" disabled={busy || data.next_before === null} onClick={() => {
      memory.write({ expanded, snapshot: data.snapshot });
      navigate({ before: data.next_before, snapshot: data.snapshot ?? route.snapshot });
    }}>查看较早记录</button></div>}
    <details className="suite-editor-help" ref={manage}><summary>采集管理{status && ` · ${PHASE[status.phase]}`}</summary>
      {status && <><div className="query-recording-status"><div><strong>{PHASE[status.phase]}</strong><span>{status.retained_rows.toLocaleString()} 条 · 数据库 {(status.database_bytes / 1048576).toFixed(1)} MiB</span></div><div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void change()}>{status.enabled ? '关闭记录' : '开启记录'}</button><button className="btn btn-danger btn-sm" disabled={busy || !status.retained_rows} onClick={() => setConfirmClear(true)}>清空记录</button></div></div>
        {!status.enabled && <Notice>开启后会保存来源 IP 和查询域名。关闭记录会保留已有记录，仍按保留期限清理。</Notice>}
        {status.phase === 'unavailable' && <Notice tone="warning">当前 DNS 入口的记录不可用。请检查 DNS 入口、采集服务及权限。</Notice>}
        {confirmClear && <Notice tone="warning"><p>清空全部查询记录及等待中的采集事件？</p><div className="suite-button-row"><button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void change(true)}>确认清空记录</button><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setConfirmClear(false)}>取消</button></div></Notice>}
      </>}
      <p className="suite-note">来源为 DNS 入口看到的 IP，路由器代转时可能只显示路由器。UDP 与 TCP 查询分别计数。MosDNS 事件记录核心处理时间，报文采集记录请求与应答之间的间隔。</p>
      <p className="suite-note">最多保留 24 小时及 100,000 条，数据库上限 31 MiB，总存储预算 64 MiB。每条最多展示 4 个 A / AAAA 地址。</p>
      {status && <p className="suite-note">本次采集计数 {Object.entries(status.counters).map(([key, value]) => `${COUNTERS[key] ?? key} ${value}`).join(' · ') || '尚无事件'}</p>}
    </details>
  </Section>;
}

export default memo(RecentQueries);

type DeviceEditor = { draft: string | null; setDraft: (name: string | null) => void };

export function QueryDetails({ event, route, deviceEditor }: { event: QueryEvent; route: DiagnosticRoute; deviceEditor?: DeviceEditor }) {
  const { caps } = useSuiteConfig();
  const { apply, task } = useFilterView();
  const { devices, allowed } = useSourceDevices();
  const [localDraft, setLocalDraft] = useState<string | null>(null);
  const draft = deviceEditor ? deviceEditor.draft : localDraft;
  const setDraft = deviceEditor?.setDraft ?? setLocalDraft;
  const deviceName = devices[event.source_ip];
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
      const result = await apply({ action: 'allow', domain: event.domain, mode });
      if (controller.signal.aborted) return;
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
    <h4>{event.domain}</h4>
    <div className="query-detail-verdict">
      <span>查询时判定</span>
      <strong data-action={verdict?.action}>{verdict ? filterLabels[verdict.action] : '未取得判定'}</strong>
      {verdict?.rule && <div><span>命中规则</span><code>{verdict.rule}</code>
        {verdict.source && <span title={verdict.source_id}>来自 {verdict.source}</span>}</div>}
    </div>
    <div className="query-detail-device">
      <span>设备来源</span>
      <a href={diagnosticHash({ ...route, source: event.source_ip, before: null })}>
        <SourceAddress ip={event.source_ip} name={deviceName} /></a>
      <span>端口 {event.source_port}</span>
      {allowed && <button type="button" className="btn btn-secondary btn-sm" aria-expanded={draft !== null}
        onClick={() => setDraft(draft === null ? deviceName ?? '' : null)}>{deviceName ? '编辑备注' : '添加备注'}</button>}
    </div>
    {draft !== null && <SourceDeviceEditor address={event.source_ip} name={deviceName} onClose={() => setDraft(null)}
      draft={{ value: draft, onChange: setDraft }} />}
    {notice && <Notice tone={error ? 'error' : undefined}>{notice}</Notice>}
    <div className="suite-button-row query-detail-actions">
    {verdict?.action === 'block' && caps?.features.dns_filter && <>
      <a className="btn btn-secondary btn-sm" href={`#/filter?tab=records&check=${encodeURIComponent(event.domain)}`}>检查当前规则</a>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy || task?.phase === 'pending'} onClick={() => void allow('temporary')}>精确放行 10 分钟</button>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy || task?.phase === 'pending'} onClick={() => void allow('permanent')}>永久放行此域名</button>
    </>}
    {RECORD_TYPES.some(type => type === event.record_type) &&
      <a className="btn btn-secondary btn-sm query-record-query"
        href={diagnosticHash({ ...route, tab: 'query', domain: event.domain, record: event.record_type })}
        aria-label={`主动查询 ${event.domain} ${event.record_type}`}>重新查询</a>}
    {verdict?.action === 'block' && caps?.features.dns_filter && <span className="suite-note">精确放行作用于整个实例。</span>}
    </div>
    <div className="query-detail-columns">
      <dl><div><dt>查询类型</dt><dd>{event.record_type} · {event.transport.toUpperCase()}</dd></div>
        <div><dt>解析入口</dt><dd><code>{dnsAddress(event.entry_address, event.entry_port)}</code> · {event.component}</dd></div>
        {event.same_key_packets > 1 && <div><dt>同键请求</dt><dd>{event.same_key_packets} 次</dd></div>}
      </dl>
      <dl><div><dt>应答</dt><dd>{responseText(event)} · {addressText(event)}</dd></div>
        <div><dt>应答时间</dt><dd>{event.response_at === null ? '—' : date(event.response_at)}</dd></div>
        <div><dt>响应耗时</dt><dd>{elapsedText(event)} · {event.timing_scope === 'mosdns_entry_processing' ? 'MosDNS 核心处理' : '入口报文间隔'}</dd></div>
        {verdict && <div><dt>判定版本</dt><dd><details className="query-detail-version"><summary aria-label="查看完整判定版本"><code>{verdict.revision.slice(0, 12)}</code></summary><code>{verdict.revision}</code></details></dd></div>}
      </dl>
    </div>
  </div>;
}

function QueryFilters({ route, busy, onApply }: { route: DiagnosticRoute; busy: boolean; onApply: (patch: Partial<DiagnosticRoute>) => void }) {
  const [source, setSource] = useState(route.source);
  const [domain, setDomain] = useState(route.filterDomain);
  const [qtype, setQtype] = useState(route.qtype);
  const [response, setResponse] = useState(route.response);
  const [shown, setShown] = useState(false);
  const active = [route.source, route.filterDomain, route.qtype, route.response].filter(Boolean).length;
  const signature = JSON.stringify([route.source, route.filterDomain, route.qtype, route.response]);
  const [previous, setPrevious] = useState(signature);
  if (signature !== previous) {
    setPrevious(signature);
    setSource(route.source); setDomain(route.filterDomain); setQtype(route.qtype); setResponse(route.response);
  }
  const clear = () => {
    setSource(''); setDomain(''); setQtype(''); setResponse('');
    onApply({ source: '', filterDomain: '', exactDomain: '', qtype: '', response: '', decision: '' });
  };
  return <>
    <div className="query-filter-mobile suite-button-row">
      <button type="button" className="btn btn-secondary btn-sm" aria-expanded={shown} aria-controls="query-record-filters"
        onClick={() => setShown(!shown)}>筛选{active > 0 && `（${active} 项）`}</button>
      {(active > 0 || route.exactDomain || route.decision) && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={clear}>清除筛选</button>}
    </div>
    <form id="query-record-filters" className="query-record-filters" data-mobile-open={shown}
      onSubmit={e => { e.preventDefault(); onApply({ source: source.trim(), filterDomain: domain.trim(), qtype, response }); }}>
    <label>来源 IP<input className="form-input" aria-label="筛选来源 IP" maxLength={45} value={source} onChange={e => setSource(e.target.value)} placeholder="完整 IPv4 / IPv6" /></label>
    <label>域名包含<input className="form-input" maxLength={253} aria-label="筛选查询域名" value={domain} onChange={e => setDomain(e.target.value)} placeholder="example.com" /></label>
    <label>类型<select className="form-select" aria-label="筛选查询类型" value={qtype} onChange={e => setQtype(e.target.value)}><option value="">全部</option>{FILTER_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}{qtype && !FILTER_TYPES.some(([value]) => value === qtype) && <option value={qtype}>类型 {qtype}</option>}</select></label>
    <label>响应<select className="form-select" aria-label="筛选响应" value={response} onChange={e => setResponse(e.target.value)}><option value="">全部</option>
      {RESPONSES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      {response && !RESPONSES.some(([value]) => value === response) && <option value={response}>{response}</option>}
    </select></label>
    <div className="suite-button-row"><button className="btn btn-secondary" disabled={busy}>筛选</button>
      <button type="button" className="btn btn-secondary" disabled={busy} onClick={clear}>清除筛选</button>
    </div>
  </form></>;
}
