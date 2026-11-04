import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, AlertTriangle, CheckCircle, Clock, Database, RefreshCw, Search, Server, Shield } from 'lucide-react';
import { suiteFetch } from '../suiteApi';
import type { Capabilities, Revision, RuntimeSnapshot, SuiteDnsResult } from '../suiteApi';
import SuiteMaintenance from '../components/SuiteMaintenance';
import './SuiteOverview.css';

const FILES = ['custom_env.ini', 'custom_mod.yaml', 'unbound_custom.conf', 'force_forward_list.txt',
  'force_dnscrypt_list.txt', 'force_recurse_list.txt', 'force_ttl_rules.txt', 'custom_cn_mark.txt', 'trackerslist.txt'];
const RECORDS = ['A', 'AAAA', 'CNAME', 'NS', 'MX', 'TXT', 'SRV', 'PTR', 'SVCB', 'HTTPS'];

function memory(value: unknown) {
  return typeof value === 'number' ? `${(value / 1024 / 1024).toFixed(1)} MiB` : '未采集';
}

export default function SuiteOverview() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [runtime, setRuntime] = useState<RuntimeSnapshot | null>(null);
  const [error, setError] = useState('');
  const [runtimeError, setRuntimeError] = useState('');
  const [token, setToken] = useState(() => { try { return localStorage.getItem('paopaodns-token') ?? ''; } catch { return ''; } });
  const [domain, setDomain] = useState('www.baidu.com');
  const [record, setRecord] = useState('A');
  const [dnsPort, setDnsPort] = useState('53');
  const portEdited = useRef(false);
  const [query, setQuery] = useState<SuiteDnsResult | null>(null);
  const [queryError, setQueryError] = useState('');
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(FILES[0]);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [historyError, setHistoryError] = useState('');
  const [preview, setPreview] = useState<{ filename: string; revision: number; content: string } | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [logs, setLogs] = useState('');
  const [component, setComponent] = useState('mosdns');
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const result = await suiteFetch<Capabilities>('/api/capabilities');
      if (!mounted.current) return;
      setCaps(result); setError('');
      if (!portEdited.current && result.dns_defaults) {
        setDnsPort(String(result.dns_defaults.port));
      }
      try {
        const sample = await suiteFetch<RuntimeSnapshot>('/api/runtime');
        if (mounted.current) { setRuntime(sample); setRuntimeError(''); }
      } catch (e) {
        if (mounted.current) { setRuntime(null); setRuntimeError(String(e)); }
      }
    } catch (e) {
      if (mounted.current) { setCaps(null); setRuntime(null); setError(String(e)); }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await refresh(); if (!stopped) timer = setTimeout(poll, 5000); };
    void poll();
    return () => { stopped = true; mounted.current = false; clearTimeout(timer); };
  }, [refresh]);

  const loadHistory = useCallback(async () => {
    setHistoryBusy(true); setHistoryError(''); setPreview(null);
    try {
      const result = await suiteFetch<{ revisions: Revision[] }>(`/api/history/${file}`);
      setRevisions(result.revisions);
    } catch (e) { setRevisions([]); setHistoryError(String(e)); }
    finally { setHistoryBusy(false); }
  }, [file]);

  const runQuery = async () => {
    setBusy(true); setQueryError('');
    try {
      setQuery(await suiteFetch<SuiteDnsResult>('/api/dns-test', { method: 'POST',
        body: JSON.stringify({ domain, record_type: record, server: '127.0.0.1', port: Number(dnsPort) }) }));
    } catch (e) { setQueryError(String(e)); }
    finally { setBusy(false); }
  };

  const restore = async () => {
    if (!preview || preview.filename !== file) return;
    setHistoryBusy(true); setHistoryError('');
    try {
      const result = await suiteFetch<{ message: string; condition: string }>(
        `/api/history/${file}/${preview.revision}/restore`, { method: 'POST' });
      await loadHistory(); setHistoryError(`${result.message}。${result.condition}`);
    } catch (e) { setHistoryError(String(e)); }
    finally { setHistoryBusy(false); }
  };

  return <div className="suite-overview">
    <div className="page-header page-header-row"><div><h2>LovestDNS</h2>
      <p>核心运行状态、缓存统计与配置历史</p></div>
      <button className="btn btn-secondary btn-sm" onClick={() => void refresh()}><RefreshCw size={14} />刷新</button></div>
    <div className="card suite-access"><Shield size={18} /><label htmlFor="suite-token">管理 Token</label>
      <input id="suite-token" type="password" className="form-input" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
      <button className="btn btn-primary btn-sm" onClick={() => { try { localStorage.setItem('paopaodns-token', token); void refresh(); }
        catch { setError('浏览器无法保存 Token'); } }}>连接</button></div>
    {error && <div className="card" role="alert">{error}</div>}
    {caps && <>
      <div className="suite-summary">
        <div className="card"><Server size={20} /><span>核心进程</span><strong>{runtime ? runtime.core_healthy ? '组件运行中' : runtime.ready ? '组件异常' : '正在初始化' : '状态不可用'}</strong>
          <small>进程状态与统计；解析结果请执行下方查询</small></div>
        <div className="card"><Activity size={20} /><span>集成版本</span><strong>{caps.version}</strong><small>核心来源 {caps.core.source_commit.slice(0, 12)}</small></div>
        <div className="card"><Database size={20} /><span>Redis 使用内存</span><strong>{memory(runtime?.redis?.info?.used_memory)}</strong>
          <small>最近快照：{typeof runtime?.redis?.info?.rdb_last_save_time === 'number'
            ? new Date(runtime.redis.info.rdb_last_save_time * 1000).toLocaleString() : '未采集'}</small></div>
      </div>
      {runtimeError && <div className="card" role="status"><AlertTriangle size={16} /> {runtimeError}；暂不展示过期统计。</div>}
      <div className="suite-layer-grid">{(['recursive', 'forward'] as const).map((name) => {
        const layer = runtime?.layers[name];
        return <div className="card" key={name}><div className="card-title"><Database size={17} />{name === 'recursive' ? '递归侧 Unbound' : '转发侧 Unbound'}</div>
          {layer?.available ? <dl className="suite-counters"><div><dt>本层 QPS</dt><dd>{layer.qps ?? '等待下一次采样'}</dd></div>
            <div><dt>本层缓存命中率</dt><dd>{layer.cache_hit_ratio == null ? '暂无可比较请求' : `${(layer.cache_hit_ratio * 100).toFixed(1)}%`}</dd></div>
            <div><dt>累计查询</dt><dd>{layer.counters?.['total.num.queries']?.toLocaleString() ?? '未采集'}</dd></div>
            <div><dt>当前递归队列</dt><dd>{layer.counters?.['total.requestlist.current.all'] ?? '未采集'}</dd></div>
          </dl> : <p className="card-desc">{layer?.error ?? '等待核心准备与采集'}</p>}
          <p className="suite-note">统计仅代表本层处理的请求，不等于客户端总流量。</p></div>;
      })}</div>
      <div className="card"><div className="card-title"><Activity size={17} />组件状态</div>
        <div className="suite-components">{Object.entries(runtime?.components ?? {}).map(([name, value]) => <div key={name}>
          {value.statename === 'RUNNING' ? <CheckCircle size={14} /> : <Clock size={14} />}<code>{name}</code><span>{value.statename}</span></div>)}</div>
        <p className="suite-note">更新任务正常结束后显示 EXITED。采集时间：{runtime ? new Date(runtime.sampled_at * 1000).toLocaleString() : '未采集'}</p></div>
      <div className="card"><div className="card-title"><Search size={17} />内部 DNS 查询</div>
        <form className="suite-query" onSubmit={(e) => { e.preventDefault(); void runQuery(); }}>
          <input aria-label="查询域名" className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} />
          <select aria-label="记录类型" className="form-select" value={record} onChange={(e) => setRecord(e.target.value)}>{RECORDS.map((type) => <option key={type}>{type}</option>)}</select>
          <input aria-label="DNS 服务端口" className="form-input suite-port" type="number" min={1} max={65535} value={dnsPort} onChange={(e) => { portEdited.current = true; setDnsPort(e.target.value); }} />
          <button className="btn btn-primary" disabled={busy}>{busy ? '查询中…' : '查询'}</button></form>
        {queryError && <p role="alert">{queryError}</p>}
        {query && <><p>{query.rcode ?? query.status} · {query.elapsed_ms ?? '—'} ms · {query.transport?.toUpperCase()}</p>
          <div className="suite-scroll"><table className="info-table"><thead><tr><th>名称</th><th>类型</th><th>结果</th><th>TTL</th></tr></thead><tbody>{query.answers.map((answer, i) => <tr key={i}><td>{answer.name}</td><td>{answer.type}</td><td><code>{answer.value}</code></td><td>{answer.ttl}</td></tr>)}</tbody></table></div>
          {!!query.debug?.length && <details open><summary>核心返回的诊断标记</summary>{query.debug.map((item, i) => <p key={i}><code>{item.value}</code></p>)}
            <p className="suite-note">标记可能来自生成缓存时的查询；累计耗时不代表本次各阶段耗时。以实际 DNS 返回码为准。</p></details>}
          {query.error && <p role="alert">{query.error}</p>}</>}
        <p className="suite-note">未清空缓存。启用 ADDINFO 后可查看核心附带的路径标记；未返回标记时不推测解析路径。</p></div>
      <div className="card"><div className="card-title"><Clock size={17} />配置历史与恢复</div>
        <div className="suite-query"><select aria-label="配置文件" className="form-select" disabled={historyBusy} value={file} onChange={(e) => { setFile(e.target.value); setPreview(null); setRevisions([]); }}>{FILES.map((name) => <option key={name}>{name}</option>)}</select>
          <button className="btn btn-secondary" disabled={historyBusy} onClick={() => void loadHistory()}>读取历史</button></div>
        <p className="suite-note">记录从集成版保存配置开始。恢复文件也会记录历史；随后仍需按提示应用配置。</p>
        {historyError && <p role="status">{historyError}</p>}
        <div className="suite-revisions">{revisions.map((revision) => <button className="btn btn-secondary btn-sm" key={revision.id} disabled={historyBusy} onClick={async () => {
          setHistoryBusy(true); setHistoryError('');
          try { setPreview(await suiteFetch(`/api/history/${file}/${revision.id}`)); }
          catch (e) { setHistoryError(String(e)); } finally { setHistoryBusy(false); }
        }}>{new Date(revision.created_at * 1000).toLocaleString()} · {revision.bytes} B</button>)}</div>
        {preview && <><pre className="suite-log">{preview.content}</pre><button className="btn btn-primary" disabled={historyBusy} onClick={() => void restore()}>恢复此版本到 {file}</button></>}
      </div>
      <SuiteMaintenance caps={caps} runtime={runtime} />
      <div className="card"><div className="card-title">组件日志</div><div className="suite-query">
        <select aria-label="日志组件" className="form-select" value={component} onChange={(e) => { setComponent(e.target.value); setLogs(''); }}>{['mosdns', 'unbound-raw', 'unbound-forward', 'redis', 'dnscrypt', 'dnscrypt-socks', 'watcher'].map((name) => <option key={name}>{name}</option>)}</select>
        <button className="btn btn-secondary" onClick={async () => { try { const result = await suiteFetch<{ lines: string[]; error?: string }>(`/api/core-logs/${component}`); setLogs(result.error ?? result.lines.join('\n')); } catch (e) { setLogs(String(e)); } }}>读取最近日志</button></div>
        {logs && <pre className="suite-log">{logs}</pre>}<p className="suite-note">仅读取本组件最近 200 行，文件自动轮转。默认未启用逐条查询日志。</p></div>
    </>}
  </div>;
}
