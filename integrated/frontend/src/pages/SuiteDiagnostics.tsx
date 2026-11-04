import { useRef, useState } from 'react';
import { Download, HeartPulse, Plus, Search, Split, Upload, X } from 'lucide-react';
import { suiteFetch, errorText, downloadJson } from '../suiteApi';
import type { SuiteDnsResult, SuiteReport } from '../suiteApi';
import { RECORD_TYPES, REPORT_LIMIT, parseSuiteReport } from '../reports';
import { useSuite } from '../suiteContext';
import { Notice, Section } from '../components/SuiteUI';
import './SuitePage.css';

type Target = { label: string; server: string; port: string; transport: 'udp' | 'tcp' };
export default function SuiteDiagnostics() {
  const { caps } = useSuite();
  const [domain, setDomain] = useState('www.baidu.com');
  const [record, setRecord] = useState('A');
  const [mode, setMode] = useState<'query' | 'compare'>('query');
  const [server, setServer] = useState(''); const [port, setPort] = useState('');
  const [transport, setTransport] = useState<'udp' | 'tcp'>('udp');
  const [targets, setTargets] = useState<Target[]>([
    { label: '核心入口', server: '', port: '', transport: 'udp' },
    { label: '递归侧', server: '127.0.0.1', port: '5301', transport: 'udp' },
  ]);
  const [report, setReport] = useState<SuiteReport | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [imported, setImported] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const defaultServer = caps?.dns_defaults.server ?? '127.0.0.1';
  const defaultPort = caps?.dns_defaults.port ?? 53;

  const run = async () => {
    if (!caps || busy) return;
    setBusy(true); setError(''); setReport(null); setImported(false);
    const started = new Date().toISOString();
    try {
      if (mode === 'query') {
        const result = await suiteFetch<SuiteDnsResult>('/api/v1/dns-test', { method: 'POST', body: JSON.stringify({ domain, record_type: record, server: server || defaultServer, port: Number(port || defaultPort), transport }) });
        setReport({ kind: 'paopaodns-dns-report', schema_version: 1, started_utc: started, method: '从集成版网络执行的按需 DNS 查询；未清空缓存。', tests: [{ label: '域名查询', result }] });
      } else {
        setReport(await suiteFetch<SuiteReport>('/api/v1/dns-compare', { method: 'POST', body: JSON.stringify({ domain, record_type: record, targets: targets.map((item) => ({ ...item, server: item.server || defaultServer, port: Number(item.port || defaultPort) })) }) }));
      }
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };
  const update = (index: number, patch: Partial<Target>) => setTargets((old) => old.map((item, i) => i === index ? { ...item, ...patch } : item));
  const importReport = async (file: File) => {
    setError('');
    try { if (file.size > REPORT_LIMIT) throw new Error('报告不能超过 2 MiB'); const value = parseSuiteReport(await file.text()); setReport(value); setImported(true); }
    catch (e) { setError(errorText(e)); }
  };
  return <div className="suite-page">
    <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/monitoring">查看运行指标</a><a className="btn btn-secondary btn-sm" href="#/system">维护与日志</a></div>
    <Section title="DNS 诊断" subtitle="查询从当前 DNS 实例发起；链路可用性取决于运行模式" icon={HeartPulse}
      actions={<div className="suite-button-row"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => input.current?.click()}><Upload size={14} />导入报告</button><button className="btn btn-secondary btn-sm" disabled={!report || busy} onClick={() => report && downloadJson(report, `paopaodns-dns-${new Date(report.started_utc).getTime()}.json`)}><Download size={14} />导出报告</button><input ref={input} hidden aria-label="导入 DNS 报告" type="file" accept="application/json,.json" onChange={(e) => { const file = e.target.files?.[0]; if (file) void importReport(file); e.target.value = ''; }} /></div>}>
      <div className="suite-segment"><button className={mode === 'query' ? 'is-active' : ''} disabled={busy} onClick={() => setMode('query')}><Search size={15} />域名查询</button><button className={mode === 'compare' ? 'is-active' : ''} disabled={busy} onClick={() => setMode('compare')}><Split size={15} />链路对照</button></div>
      <form onSubmit={(e) => { e.preventDefault(); void run(); }}>
        <fieldset className="suite-diagnostic-fields" disabled={busy}>
          <div className="suite-query-form"><label>域名<input required aria-label="查询域名" className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="example.com" /></label><label>记录类型<select aria-label="查询记录类型" className="form-select" value={record} onChange={(e) => setRecord(e.target.value)}>{RECORD_TYPES.map((value) => <option key={value}>{value}</option>)}</select></label></div>
          {mode === 'query' ? <div className="suite-target-row"><label>DNS 服务<input aria-label="DNS 服务" className="form-input" value={server} placeholder={defaultServer} onChange={(e) => setServer(e.target.value)} /></label><label>端口<input aria-label="DNS 端口" type="number" min={1} max={65535} className="form-input" value={port} placeholder={String(defaultPort)} onChange={(e) => setPort(e.target.value)} /></label><label>传输<select aria-label="DNS 传输" className="form-select" value={transport} onChange={(e) => setTransport(e.target.value as 'udp' | 'tcp')}><option value="udp">UDP（截断时回退 TCP）</option><option value="tcp">TCP</option></select></label></div>
            : <><div className="suite-target-list">{targets.map((item, index) => <div className="suite-target-row" key={index}><label>链路名称<input required maxLength={40} aria-label={`链路 ${index + 1} 名称`} className="form-input" value={item.label} onChange={(e) => update(index, { label: e.target.value })} /></label><label>DNS 服务<input aria-label={`链路 ${index + 1} 服务`} className="form-input" value={item.server} placeholder={defaultServer} onChange={(e) => update(index, { server: e.target.value })} /></label><label>端口<input aria-label={`链路 ${index + 1} 端口`} type="number" min={1} max={65535} className="form-input" value={item.port} placeholder={String(defaultPort)} onChange={(e) => update(index, { port: e.target.value })} /></label><label>传输<select aria-label={`链路 ${index + 1} 传输`} className="form-select" value={item.transport} onChange={(e) => update(index, { transport: e.target.value as 'udp' | 'tcp' })}><option value="udp">UDP</option><option value="tcp">TCP</option></select></label><button type="button" className="suite-icon-button" aria-label={`删除链路 ${index + 1}`} disabled={targets.length <= 1} onClick={() => setTargets((old) => old.filter((_, i) => i !== index))}><X size={15} /></button></div>)}</div><button type="button" className="btn btn-secondary btn-sm" disabled={targets.length >= 4} onClick={() => setTargets((old) => [...old, { label: `链路 ${old.length + 1}`, server: '', port: '', transport: 'udp' }])}><Plus size={14} />添加链路（最多 4 条）</button></>}
          <div className="suite-button-row suite-run-actions"><button className="btn btn-primary" disabled={!caps || !domain.trim()}><Search size={16} />{busy ? '查询中…' : mode === 'query' ? '开始查询' : '开始对照'}</button><span className="suite-muted">{caps ? '空地址/端口使用核心入口默认值。UDP 自动支持 TCP 回退。' : '连接实例后可执行查询；离线仍可导入报告。'}</span></div>
        </fieldset>
      </form>
      {error && <Notice tone="error">{error}</Notice>}
      {report && <><div className="suite-report-heading"><strong>{imported ? '离线导入报告' : '本次查询结果'}</strong><time>{new Date(report.started_utc).toLocaleString('zh-CN')}</time><p>报告来源：{report.method}</p></div>{imported && <Notice>报告在本地解析，未上传。以下内容是报告记录的历史结果。</Notice>}{report.tests.map((test, index) => <Result key={index} label={test.label} result={test.result} />)}</>}
    </Section>
    <Notice>应答可能来自缓存，链路对照不会清空缓存。RCODE 以报文为准；ADDINFO 可能来自生成缓存时的历史查询。查询成功不代表网站可访问或每条规则已经命中。</Notice>
  </div>;
}

function Result({ label, result }: { label: string; result: SuiteDnsResult }) {
  return <div className="suite-result"><div className="suite-result-head"><div><span className="suite-kicker">{label} · {result.record_type}</span><h3>{result.domain}</h3></div><strong className={result.available ? 'suite-result-ok' : 'suite-result-fail'}>{result.rcode ?? result.status ?? '未知'}</strong></div><div className="suite-result-meta"><span>{result.server}:{result.port}</span><span>{result.available ? '已取得目标记录' : '未取得目标记录'}</span><span>{result.elapsed_ms ?? '—'} ms</span><span>{result.transport?.toUpperCase() ?? '—'}{result.tcp_fallback ? ' · UDP 截断后回退' : ''}</span></div>{result.error && <Notice tone="warning">{result.error}</Notice>}{result.answers.length ? <div className="suite-records">{result.answers.map((answer, index) => <div key={index}><code>{answer.type}</code><span><small>{answer.name}</small>{answer.value}</span><b>{answer.ttl}s</b></div>)}</div> : <p className="suite-muted">没有返回完整目标记录</p>}{!!result.debug?.length && <details className="suite-debug" open><summary>核心诊断标记</summary>{result.debug.map((item, index) => <code key={index}>{item.value}</code>)}</details>}</div>;
}
