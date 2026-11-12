import { useEffect, useRef, useState } from 'react';
import { Download, HeartPulse, Plus, Search, Split, Upload, X } from 'lucide-react';
import { suiteFetch, errorText, downloadJson } from '../suiteApi';
import type { SuiteDnsResult, SuiteReport } from '../suiteApi';
import { RECORD_TYPES, REPORT_LIMIT, parseSuiteReport } from '../reports';
import { useSuite } from '../suiteContext';
import { Notice, Section } from '../components/SuiteUI';
import { diagnosticTargets, dnsAddress } from '../dnsListeners';
import './SuitePage.css';
import './HomeWorkflow.css';

type Target = { label: string; server: string; port: string; transport: 'udp' | 'tcp' };
export default function SuiteDiagnostics() {
  const { caps, runtime, paused, runtimeStale } = useSuite();
  const [domain, setDomain] = useState(() => new URLSearchParams(window.location.hash.split('?')[1]).get('domain') ?? 'www.baidu.com');
  const [record, setRecord] = useState('A');
  const [mode, setMode] = useState<'query' | 'compare'>('query');
  const [server, setServer] = useState(''); const [port, setPort] = useState('');
  const [transport, setTransport] = useState<'udp' | 'tcp'>('udp');
  const [targets, setTargets] = useState<Target[]>([
    { label: '默认目标 UDP', server: '', port: '', transport: 'udp' },
    { label: '默认目标 TCP', server: '', port: '', transport: 'tcp' },
  ]);
  const [report, setReport] = useState<SuiteReport | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [imported, setImported] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const defaultServer = caps?.dns_defaults.server ?? '127.0.0.1';
  const defaultPort = caps?.dns_defaults.port ?? 53;
  const presets = diagnosticTargets(caps, paused || runtimeStale ? null : runtime);

  const run = async () => {
    if (!caps || busy) return;
    setBusy(true); setError('');
    const abort = new AbortController(); request.current = abort;
    const started = new Date().toISOString();
    try {
      if (mode === 'query') {
        const result = await suiteFetch<SuiteDnsResult>('/api/v1/dns-test', { signal: abort.signal, method: 'POST', body: JSON.stringify({ domain, record_type: record, server: server || defaultServer, port: Number(port || defaultPort), transport }) });
        if (abort.signal.aborted) return;
        setReport({ kind: 'paopaodns-dns-report', schema_version: 1, started_utc: started, method: '从集成版网络执行的按需 DNS 查询；未清空缓存。', tests: [{ label: '域名查询', result }] });
      } else {
        const value = await suiteFetch<SuiteReport>('/api/v1/dns-compare', { signal: abort.signal, method: 'POST', body: JSON.stringify({ domain, record_type: record, targets: targets.map((item) => ({ ...item, server: item.server || defaultServer, port: Number(item.port || defaultPort) })) }) });
        if (abort.signal.aborted) return;
        setReport(value);
      }
      setImported(false);
    } catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  const update = (index: number, patch: Partial<Target>) => setTargets((old) => old.map((item, i) => i === index ? { ...item, ...patch } : item));
  const importReport = async (file: File) => {
    setError('');
    try { if (file.size > REPORT_LIMIT) throw new Error('报告不能超过 2 MiB'); const value = parseSuiteReport(await file.text()); setReport(value); setImported(true); }
    catch (e) { setError(errorText(e)); }
  };
  return <div className="suite-page">
    <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/status">查看服务状态</a><a className="btn btn-secondary btn-sm" href="#/system">维护与日志</a></div>
    <Section title="DNS 诊断" subtitle="查询从当前 DNS 实例发起；链路可用性取决于运行模式" icon={HeartPulse}
      actions={<div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => input.current?.click()}><Upload size={14} />导入报告</button><button className="btn btn-secondary btn-sm" disabled={!report || busy} onClick={() => report && downloadJson(report, `paopaodns-dns-${new Date(report.started_utc).getTime()}.json`)}><Download size={14} />导出报告</button><input ref={input} hidden aria-label="导入 DNS 报告" type="file" accept="application/json,.json" onChange={(e) => { const file = e.target.files?.[0]; if (file) void importReport(file); e.target.value = ''; }} /></div>}>
      <div className="suite-segment"><button className={mode === 'query' ? 'is-active' : ''} disabled={busy} onClick={() => setMode('query')}><Search size={15} />域名查询</button><button className={mode === 'compare' ? 'is-active' : ''} disabled={busy} onClick={() => setMode('compare')}><Split size={15} />链路对照</button></div>
      <form onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <fieldset className="suite-diagnostic-fields" disabled={busy}>
          {mode === 'query' && <div className="suite-diagnostic-presets"><label htmlFor="diagnostic-target">选择目标</label><select id="diagnostic-target" className="form-select" value="" onChange={(event) => { const target = presets.find((item) => item.id === event.target.value); if (target) { setServer(target.server); setPort(String(target.port)); setTransport(target.transport); } }}><option value="">选择预设，或下方手动填写</option>{presets.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select><span className="suite-note">通道预设来自当前监听采集。</span></div>}
          <div className="suite-query-form"><label>域名<input required aria-label="查询域名" className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" /></label><label>记录类型<select aria-label="查询记录类型" className="form-select" value={record} onChange={(e) => setRecord(e.target.value)}>{RECORD_TYPES.map((value) => <option key={value}>{value}</option>)}</select></label></div>
          {mode === 'query' ? <div className="suite-target-row"><label>DNS 服务<input aria-label="DNS 服务" className="form-input" value={server} placeholder={defaultServer} onChange={(e) => setServer(e.target.value)} /></label><label>端口<input aria-label="DNS 端口" type="number" min={1} max={65535} className="form-input" value={port} placeholder={String(defaultPort)} onChange={(e) => setPort(e.target.value)} /></label><label>传输<select aria-label="DNS 传输" className="form-select" value={transport} onChange={(e) => setTransport(e.target.value as 'udp' | 'tcp')}><option value="udp">UDP（截断时回退 TCP）</option><option value="tcp">TCP</option></select></label></div>
            : <><div className="suite-target-list">{targets.map((item, index) => <div className="suite-target-row" key={index}><label>链路名称<input required maxLength={40} aria-label={`链路 ${index + 1} 名称`} className="form-input" value={item.label} onChange={(e) => update(index, { label: e.target.value })} /></label><label>DNS 服务<input aria-label={`链路 ${index + 1} 服务`} className="form-input" value={item.server} placeholder={defaultServer} onChange={(e) => update(index, { server: e.target.value })} /></label><label>端口<input aria-label={`链路 ${index + 1} 端口`} type="number" min={1} max={65535} className="form-input" value={item.port} placeholder={String(defaultPort)} onChange={(e) => update(index, { port: e.target.value })} /></label><label>传输<select aria-label={`链路 ${index + 1} 传输`} className="form-select" value={item.transport} onChange={(e) => update(index, { transport: e.target.value as 'udp' | 'tcp' })}><option value="udp">UDP</option><option value="tcp">TCP</option></select></label><button type="button" className="suite-icon-button" aria-label={`删除链路 ${index + 1}`} disabled={targets.length <= 1} onClick={() => setTargets((old) => old.filter((_, i) => i !== index))}><X size={15} /></button></div>)}</div><button type="button" className="btn btn-secondary btn-sm" disabled={targets.length >= 4} onClick={() => setTargets((old) => [...old, { label: `链路 ${old.length + 1}`, server: '', port: '', transport: 'udp' }])}><Plus size={14} />添加链路（最多 4 条）</button></>}
          <div className="suite-button-row suite-run-actions"><button className="btn btn-primary" disabled={!caps || !domain.trim()}><Search size={16} />{busy ? '查询中…' : mode === 'query' ? '开始查询' : '开始对照'}</button><span className="suite-muted">{caps ? '空地址/端口使用默认诊断目标。UDP 自动支持 TCP 回退。' : '连接实例后可执行查询；离线仍可导入报告。'}</span></div>
        </fieldset>
      </form>
      {error && <Notice tone="error">{error}</Notice>}
      {report && <><div className="suite-report-heading"><strong>{busy ? '正在查询，以下保留上次结果' : imported ? '离线导入报告' : '查询结果'}</strong><time>{new Date(report.started_utc).toLocaleString('zh-CN')}</time><p>报告来源：{report.method}</p></div>{imported && <Notice>报告在本地解析，未上传。以下内容是报告记录的历史结果。</Notice>}<ReportSummary report={report} />{report.tests.map((test, index) => <Result key={index} label={test.label} result={test.result} />)}</>}
    </Section>
    <details className="suite-editor-help"><summary>统计与标记说明</summary><p className="suite-note">主动查询耗时不代表持续服务统计。链路对照不清空缓存；ADDINFO 可能来自生成缓存时的历史查询，未返回标记时不推测路径。RCODE 以报文为准。</p></details>
  </div>;
}

function ReportSummary({ report }: { report: SuiteReport }) {
  const signatures = report.tests.map(({ result }) => JSON.stringify([result.rcode, result.status, result.answers.map((answer) => `${answer.name}|${answer.type}|${answer.value}`).sort()]));
  const agree = new Set(signatures).size === 1;
  return <div className="suite-report-summary"><strong>{report.tests.length > 1 ? agree ? '各链路的记录与响应状态一致' : '链路结果存在差异，请检查下方记录' : report.tests[0]?.result.available ? '已取得目标记录' : '未取得目标记录，请检查响应状态'}</strong><div className="suite-table-scroll"><table className="info-table"><thead><tr><th>链路</th><th>响应</th><th>记录数</th><th>耗时</th></tr></thead><tbody>{report.tests.map(({ label, result }, i) => <tr key={i}><td>{label}</td><td>{result.rcode ?? result.status ?? '未知'}</td><td>{result.answers.length}</td><td>{result.elapsed_ms ?? '—'} ms</td></tr>)}</tbody></table></div><p className="suite-note">比较响应状态及记录类型、名称和值；TTL 和返回顺序可能因缓存不同。</p></div>;
}

function Result({ label, result }: { label: string; result: SuiteDnsResult }) {
  return <div className="suite-result"><div className="suite-result-head"><div><span className="suite-kicker">{label} · {result.record_type}</span><h3>{result.domain}</h3></div><strong className={result.available ? 'suite-result-ok' : result.rcode === 'NOERROR' ? 'suite-result-neutral' : 'suite-result-fail'}>{result.rcode ?? result.status ?? '未知'}</strong></div><div className="suite-result-meta"><span>{dnsAddress(result.server, result.port)}</span><span>{result.available ? '已取得目标记录' : '未取得目标记录'}</span><span>{result.elapsed_ms ?? '—'} ms</span><span>{result.transport?.toUpperCase() ?? '—'}{result.tcp_fallback ? ' · UDP 截断后回退' : ''}</span></div>{result.error && <Notice tone="warning">{result.error}</Notice>}{result.answers.length ? <div className="suite-records">{result.answers.map((answer, index) => <div key={index}><code>{answer.type}</code><span><small>{answer.name}</small>{answer.value}</span><b>{answer.ttl}s</b></div>)}</div> : <p className="suite-muted">没有返回完整目标记录</p>}{!!result.debug?.length && <details className="suite-debug"><summary>核心诊断标记</summary>{result.debug.map((item, index) => <code key={index}>{item.value}</code>)}</details>}</div>;
}
