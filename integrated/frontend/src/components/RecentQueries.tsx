import { useCallback, useEffect, useRef, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import { recentQueries, recordingValid } from '../queryRecords';
import type { QueryRecords, QueryRecording } from '../queryRecords';
import { Empty, Notice, Section } from './SuiteUI';
import { dnsAddress } from '../dnsListeners';
import './RecentQueries.css';

const PHASE = { disabled: '采集已关闭', starting: '正在启动采集', recording: '正在采集', unavailable: '采集暂不可用' };
const DROP_KEYS = ['kernel_drops', 'queue_drops', 'storage_drops', 'pending_evictions', 'stream_evictions', 'tcp_gaps', 'discarded_pending'];
const COUNTERS: Record<string, string> = { queries_observed: '入口事务', stored_events: '已保存', kernel_drops: '内核丢包', queue_drops: '队列丢弃', storage_drops: '保存丢弃', storage_errors: '存储错误', pending_evictions: '待响应容量丢弃', stream_evictions: 'TCP 容量丢弃', tcp_gaps: 'TCP 缺口', discarded_pending: '关联重置丢弃', discarded_events: '启停或清空丢弃', unparsed_packets: '未解析报文', tcp_untracked_packets: '未跟踪 TCP 报文', unmatched_responses: '未配对响应', no_response_observed: '未观察到响应', same_key_queries: '同键请求', invalid_timing: '无效计时', clock_resets: '时钟跳变重置', tcp_retransmitted_packets: 'TCP 重传报文', tcp_idle_expirations: 'TCP 跟踪到期', capture_errors: '采集错误' };
export default function RecentQueries() {
  const { caps, paused } = useSuite();
  const instanceId = caps?.instance_id ?? '';
  const [data, setData] = useState<QueryRecords | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [source, setSource] = useState(''); const [domain, setDomain] = useState(''); const [type, setType] = useState('');
  const [filters, setFilters] = useState('limit=50'); const [before, setBefore] = useState<number | null>(null);
  const [automatic, setAutomatic] = useState(false); const [confirmClear, setConfirmClear] = useState(false);
  const request = useRef<AbortController | null>(null);
  const loadedInstance = useRef('');
  const allowed = caps?.features.query_recording === true && caps.permissions.configure;
  const load = useCallback(async () => {
    if (!allowed) return;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    if (loadedInstance.current !== instanceId) { setData(null); loadedInstance.current = instanceId; }
    setBusy(true);
    const params = new URLSearchParams(filters);
    if (before !== null) params.set('before', String(before));
    try {
      const result = await recentQueries(params, controller.signal);
      if (result.instance_id !== instanceId) throw new Error('查询记录的实例身份已变化，请刷新服务信息');
      if (!controller.signal.aborted) { setData(result); setError(''); }
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }, [allowed, filters, before, instanceId]);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => { window.clearTimeout(timer); request.current?.abort(); }; }, [load]);
  useEffect(() => {
    if (!automatic || paused || before !== null) return;
    const timer = window.setInterval(() => { if (!document.hidden && !busy) void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [automatic, paused, before, busy, load]);
  const change = async (clear = false) => {
    if (!data || busy) return;
    setBusy(true); setError(''); request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    try {
      const status = await suiteFetch<QueryRecording>(clear ? '/api/v1/query-records' : '/api/v1/query-recording', {
        method: clear ? 'DELETE' : 'PUT', signal: controller.signal,
        ...(clear ? {} : { body: JSON.stringify({ enabled: !data.status.enabled }) }),
      });
      if (!recordingValid(status)) throw new Error('采集状态响应格式无效');
      if (!controller.signal.aborted) { setData(old => old ? { ...old, status, ...(clear ? { events: [], next_before: null } : {}) } : old); setConfirmClear(false); if (clear) setBefore(null); }
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  if (!allowed) return null;
  const status = data?.status;
  const losses = status ? DROP_KEYS.reduce((total, key) => total + (status.counters[key] ?? 0), 0) : 0;
  return <Section title="最近查询" subtitle="当前实例入口的真实 DNS 报文，默认关闭采集" icon={Database}
    actions={<button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void load()}><RefreshCw size={14} />{busy ? '读取中…' : '刷新记录'}</button>}>
    {error && <Notice tone="error">{error}{data && '；下方保留上次结果。'}</Notice>}
    {status && <><div className="query-recording-status"><div><strong>{PHASE[status.phase]}</strong><span>{status.retained_rows.toLocaleString()} 条 · 数据库 {(status.database_bytes / 1048576).toFixed(1)} MiB</span></div><div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void change()}>{status.enabled ? '关闭采集' : '开启采集'}</button><button className="btn btn-secondary btn-sm" disabled={busy || !status.retained_rows} onClick={() => setConfirmClear(true)}>清空记录</button></div></div>
      {!status.enabled && <Notice>开启后会保存来源 IP 和查询域名。关闭采集会保留已有记录，仍按保留期限清理。</Notice>}
      {status.phase === 'unavailable' && <Notice tone="warning">尚未确认 DNS 入口，或当前部署缺少 Linux 报文采集权限。解析服务可继续运行，请检查采集服务状态。</Notice>}
      {confirmClear && <Notice tone="warning"><p>清空全部查询记录及等待中的采集事件？</p><div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void change(true)}>确认清空记录</button><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => setConfirmClear(false)}>取消</button></div></Notice>}
      <form className="query-record-filters" onSubmit={e => { e.preventDefault(); const params = new URLSearchParams({ limit: '50' }); if (source.trim()) params.set('source', source.trim()); if (domain.trim()) params.set('domain', domain.trim()); if (type) params.set('qtype', type); if (before === null && filters === params.toString()) void load(); else { setBefore(null); setFilters(params.toString()); } }}>
        <label>来源 IP<input className="form-input" aria-label="筛选来源 IP" value={source} onChange={e => setSource(e.target.value)} placeholder="完整 IPv4 / IPv6" /></label><label>域名包含<input className="form-input" maxLength={253} aria-label="筛选查询域名" value={domain} onChange={e => setDomain(e.target.value)} placeholder="example.com" /></label><label>类型<select className="form-select" aria-label="筛选查询类型" value={type} onChange={e => setType(e.target.value)}><option value="">全部</option>{[['1', 'A'], ['28', 'AAAA'], ['5', 'CNAME'], ['16', 'TXT'], ['64', 'SVCB'], ['65', 'HTTPS']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="btn btn-secondary" disabled={busy}>筛选</button>
      </form>
      <div className="query-record-toolbar"><label><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} /> 每 5 秒刷新最近页{paused ? '（全局已暂停）' : ''}</label><span>状态采样：{new Date(status.sampled_at * 1000).toLocaleTimeString('zh-CN')}{losses > 0 && ` · ${losses} 项丢弃/缺口，记录不完整`}</span></div>
    </>}
    {data?.events.length ? <div className="suite-table-scroll"><table className="info-table query-record-table"><thead><tr><th>时间 / 来源</th><th>域名 / 类型</th><th>响应 / 地址</th><th>入口 / 观测耗时</th></tr></thead><tbody>{data.events.map(event => <tr key={event.id}><td><time>{new Date(event.observed_at * 1000).toLocaleString('zh-CN')}</time><code>{event.source_ip}</code></td><td><strong>{event.domain}</strong><span>{event.record_type} · {event.transport.toUpperCase()}{event.same_key_packets > 1 && ` · 同键请求 ${event.same_key_packets} 次`}</span></td><td><strong>{event.outcome === 'no_response_observed' ? '未观察到响应' : event.rcode}{event.truncated ? ' · 报文截断' : ''}</strong><span>{event.addresses.length ? event.addresses.join(' · ') + (event.addresses_complete ? '' : '（部分）') : event.answer_count === 0 ? '无 Answer 记录' : '无可展示 A / AAAA 地址'}</span></td><td><code>{dnsAddress(event.entry_address, event.entry_port)}</code><span>{event.elapsed_ms === null ? '—' : `${event.elapsed_ms} ms`}</span></td></tr>)}</tbody></table></div>
      : <Empty title={busy ? '正在读取查询记录' : '暂无符合条件的记录'}>{status?.enabled ? '确认客户端请求已到达当前入口，或调整筛选条件。' : '开启采集后，新的入口请求会显示在这里。'}</Empty>}
    {data && <div className="suite-button-row query-record-pages"><button className="btn btn-secondary btn-sm" disabled={busy || before === null} onClick={() => setBefore(null)}>返回最近</button><button className="btn btn-secondary btn-sm" disabled={busy || data.next_before === null} onClick={() => setBefore(data.next_before)}>查看较早记录</button></div>}
    <details className="suite-editor-help"><summary>采集范围与保留规则</summary><p className="suite-note">来源是当前服务网络命名空间看到的 IP，NAT 后可能是路由器；本机请求可能包含诊断。仅记录已确认 DNS 入口，UDP 与 TCP 回退分别计为报文事务。耗时是入口请求到响应的内核时间戳间隔；未观察到响应不等于 DNS 超时。</p><p className="suite-note">最多保留 24 小时、100,000 条；数据库上限 31 MiB，含事务日志的存储预算 64 MiB，先到者生效。仅展示最多 4 个 A / AAAA 地址。缓存命中与分流路径未知；不采集加密 DNS，IP 分片、IPv6 扩展头和采集中途已建立的 TCP 流不支持。</p>{status && <p className="suite-note">本次采集计数：{Object.entries(status.counters).map(([key, value]) => `${COUNTERS[key] ?? '其他事件'} ${value}`).join(' · ') || '尚无事件'}</p>}</details>
  </Section>;
}
