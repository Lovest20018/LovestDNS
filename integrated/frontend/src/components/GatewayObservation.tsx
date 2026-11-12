import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Cable } from 'lucide-react';
import { suiteFetch, errorText, when, ApiError } from '../suiteApi';
import type { GatewaySummary, GatewayConnections, GatewayFakeIP, GatewayRow } from '../gatewayApi';
import { Section, Notice } from './SuiteUI';
import GatewaySummaryView from './GatewaySummaryView';

const FEATURES = { recent: 'connection_history', history: 'persistent_history', mihomo: 'mihomo_connection_snapshots' };
type View = keyof typeof FEATURES;
const SOURCES: Record<string, string> = { sniffbox_lifecycle: '近期 TCP 连接', mihomo_snapshot: 'Mihomo 历史快照', mihomo_connection_snapshot: 'Mihomo 当前快照' };
const STATES: Record<string, string> = { connected: '已连接', failed: '失败', active: '活动', closed: '已关闭', ended: '已结束', unknown: '未知', observed_snapshot: '已观测快照' };
const DECISIONS: Record<string, string> = { status: '决策状态', source: '决策来源', rule_id: '规则 ID', rule_kind: '规则类型', reason: '原因', outbound_id: '出口', connect_result: '建连结果', application_result: '应用结果' };
const FAKE_LABELS: Record<string, string> = { status: '查询结果', source: '数据来源', domain: '域名', fakeip: 'FakeIP', cidr: '地址范围', dns_ttl_seconds: 'DNS TTL（秒）', valid_until: '有效时间', validity: '有效性依据', historical_ownership_verified: '历史归属已验证', sampled_at: '采样时间', fetched_at: '读取时间' };
function timeText(value?: number | string | null) {
  if (typeof value === 'number') return when(value);
  if (!value) return '未知';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN');
}
function handling(row: GatewayRow) {
  if (row.source.startsWith('mihomo')) return row.chains.join(' → ') || '连接链未提供';
  const outbound = row.decision.outbound_id;
  const names: Record<string, string> = { direct: '直连', 'socks5-upstream': 'SOCKS5 上游', 'openvpn-tunnel': 'OpenVPN 隧道' };
  if (row.decision.source === 'mode_dispatch') return `模式派发 · ${outbound ? names[outbound] ?? outbound : '出口未知'}`;
  return outbound ?? '处理方式未知';
}
function RecordDetails({ row }: { row: GatewayRow }) {
  return <dl className="suite-kpi-list">
    <div><dt>记录来源</dt><dd>{SOURCES[row.source] ?? row.source}</dd></div>
    <div><dt>记录 ID</dt><dd>{row.id ?? '未知'}</dd></div>
    <div><dt>连接时间</dt><dd>{timeText(row.started_at)}</dd></div>
    <div><dt>实际目标 / 上游</dt><dd>{row.actual_target ?? '未知'} / {row.upstream_peer ?? '未知'}</dd></div>
    {row.first_seen_at != null && <div><dt>首次采集</dt><dd>{timeText(row.first_seen_at)}</dd></div>}
    {row.last_seen_at != null && <div><dt>最后采集</dt><dd>{timeText(row.last_seen_at)}</dd></div>}
    {row.source.startsWith('mihomo') ? <><div><dt>Mihomo 规则</dt><dd>{row.rule ?? '未知'} {row.rule_payload}</dd></div><div><dt>连接链</dt><dd>{row.chains.join(' → ') || '未知'}</dd></div></> : Object.entries(row.decision).map(([key, value]) => <div key={key}><dt>{DECISIONS[key] ?? key}</dt><dd>{value === 'mode_dispatch' ? '模式派发' : value ?? '未知'}</dd></div>)}
  </dl>;
}
export default function GatewayObservation() {
  const [summary, setSummary] = useState<GatewaySummary | null>(null);
  const [records, setRecords] = useState<GatewayConnections | null>(null);
  const [fake, setFake] = useState<GatewayFakeIP | null>(null);
  const [view, setView] = useState<View>('recent');
  const [domain, setDomain] = useState(''); const [client, setClient] = useState('');
  const [lookup, setLookup] = useState(''); const [lookupType, setLookupType] = useState('domain');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<number | null>(null);
  const request = useRef<AbortController | null>(null);
  const read = useCallback(async (path: string, kind: 'summary' | 'records' | 'fake') => {
    request.current?.abort(); const abort = new AbortController(); request.current = abort;
    setBusy(true); setError('');
    try {
      const value = await suiteFetch<GatewaySummary | GatewayConnections | GatewayFakeIP>(path, { signal: abort.signal, timeoutMs: 25000 });
      if (abort.signal.aborted) return;
      if (kind === 'summary') setSummary(value as GatewaySummary);
      else if (kind === 'records') { setRecords(value as GatewayConnections); setExpanded(null); }
      else setFake(value as GatewayFakeIP);
      return value;
    } catch (e) {
      if (!abort.signal.aborted) { setError(errorText(e)); if (e instanceof ApiError && e.status === 409) setRecords(null); }
    } finally { if (!abort.signal.aborted) setBusy(false); }
  }, []);
  useEffect(() => {
    let disposed = false;
    const timer = window.setTimeout(async () => {
      const value = await read('/api/v1/gateway/summary', 'summary') as GatewaySummary | undefined;
      if (!value || disposed) return;
      const initial = (Object.keys(FEATURES) as View[]).find((key) => value.features[FEATURES[key]]);
      if (initial) { setView(initial); await read(`/api/v1/gateway/connections?view=${initial}`, 'records'); }
    }, 0);
    return () => { disposed = true; window.clearTimeout(timer); request.current?.abort(); };
  }, [read]);
  const connections = (next = false) => {
    const params = new URLSearchParams({ view }); if (domain) params.set('domain', domain); if (client) params.set('client', client);
    if (next && records?.next_cursor) params.set('cursor', records.next_cursor);
    if (next && records?.next_before) params.set('before', String(records.next_before));
    void read(`/api/v1/gateway/connections?${params}`, 'records');
  };
  return <Section title="连接与处理记录" subtitle="进入时读取一次 · 按需刷新" icon={Cable} actions={<button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void read('/api/v1/gateway/summary', 'summary')}>刷新网关状态</button>}>
    {error && <Notice tone="error">{error}{summary || records || fake ? '。已有结果为上次读取快照。' : ''}</Notice>}
    {!summary && !error && <p role="status">正在读取网关状态…</p>}
    {!summary && error && <button className="btn btn-secondary" disabled={busy} onClick={() => void read('/api/v1/gateway/summary', 'summary')}>重试连接</button>}
    {summary && <><GatewaySummaryView value={summary} compact />
      <form onSubmit={(e) => { e.preventDefault(); connections(); }}><fieldset disabled={busy} className="suite-diagnostic-fields"><div className="suite-target-row"><label>记录来源<select aria-label="网关记录来源" className="form-select" value={view} onChange={(e) => { setView(e.target.value as View); setRecords(null); }}><option value="recent" disabled={!summary.features.connection_history}>近期连接</option>{summary.features.persistent_history && <option value="history">持久历史</option>}{summary.features.mihomo_connection_snapshots && <option value="mihomo">Mihomo 快照</option>}</select></label><label>域名精确筛选<input aria-label="网关域名筛选" className="form-input" maxLength={253} value={domain} onChange={(e) => { setDomain(e.target.value); setRecords(null); }} placeholder="example.com" /></label><label>客户端 IP<input aria-label="网关客户端筛选" className="form-input" maxLength={45} value={client} onChange={(e) => { setClient(e.target.value); setRecords(null); }} /></label></div><button className="btn btn-primary" disabled={!summary.features[FEATURES[view]]}>{busy ? '读取中…' : '读取记录'}</button></fieldset></form>
      {records && <><p className="suite-note">{records.items.length} 条 · 采样 {timeText(records.sampled_at)} · 读取 {timeText(records.fetched_at)} · {records.freshness.stale ? '采样过期' : '非完整历史'}{records.gap ? ' · 存在记录缺口' : ''}</p>
        {records.items.length ? <div className="suite-table-scroll" tabIndex={0} role="region" aria-label="网关连接记录"><table className="info-table suite-gateway-table"><thead><tr><th>时间</th><th>客户端</th><th>域名 / 目标</th><th>处理方式</th><th>状态</th><th>详情</th></tr></thead><tbody>{records.items.map((row, i) => <Fragment key={`${row.id ?? i}-${i}`}><tr><td>{timeText(row.started_at ?? row.last_seen_at)}</td><td><code>{row.client ?? '未知'}</code></td><td><strong>{row.domain ?? row.destination ?? '未知'}</strong>{row.domain && <small>{row.destination ?? '目标 IP 未提供'}</small>}</td><td>{handling(row)}<small>{row.source.startsWith('mihomo') ? `规则：${row.rule ?? '未提供'}${row.rule_payload ? ` · ${row.rule_payload}` : ''}` : SOURCES[row.source] ?? row.source}</small></td><td>{STATES[row.state ?? row.decision.connect_result ?? ''] ?? row.state ?? row.decision.connect_result ?? '未知'}</td><td><button className="btn btn-secondary btn-sm" aria-expanded={expanded === i} aria-controls={`gateway-record-${i}`} onClick={() => setExpanded(expanded === i ? null : i)}>{expanded === i ? '收起' : '展开'}</button></td></tr>{expanded === i && <tr id={`gateway-record-${i}`} className="suite-gateway-detail"><td colSpan={6}><RecordDetails row={row} /></td></tr>}</Fragment>)}</tbody></table></div> : <p className="suite-gateway-empty">本次范围内无记录。</p>}
        <p className="suite-note">范围：{records.scope} · {records.filter_scope}</p>
        {(records.next_cursor || records.next_before) && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => connections(true)}>下一页</button>}
      </>}
      {summary.features.fakeip_lookup && <details className="suite-editor-help suite-gateway-fakeip"><summary>FakeIP 缓存查询</summary><form onSubmit={(e) => { e.preventDefault(); void read(`/api/v1/gateway/fakeip?${new URLSearchParams({ [lookupType]: lookup })}`, 'fake'); }}><fieldset disabled={busy} className="suite-diagnostic-fields"><div className="suite-target-row"><label>查询类型<select className="form-select" value={lookupType} onChange={(e) => { setLookupType(e.target.value); setFake(null); }}><option value="domain">域名</option><option value="ip">IP</option></select></label><label>查询值<input aria-label="FakeIP 查询值" className="form-input" required maxLength={253} value={lookup} onChange={(e) => { setLookup(e.target.value); setFake(null); }} /></label><button className="btn btn-secondary">查询 FakeIP</button></div></fieldset></form>
        {fake && <dl className="suite-kpi-list">{Object.entries(fake).map(([key, value]) => <div key={key}><dt>{FAKE_LABELS[key] ?? key}</dt><dd>{value == null ? '未知' : key === 'sampled_at' || key === 'fetched_at' ? timeText(value as number) : typeof value === 'boolean' ? value ? '是' : '否' : value === 'no_records' ? '当前缓存无记录' : String(value)}</dd></div>)}</dl>}
      </details>}
      <p className="suite-note">模式派发与 Mihomo 规则分别取自各自记录。观测可能不完整；FakeIP 当前映射不证明历史归属。</p>
    </>}
  </Section>;
}
