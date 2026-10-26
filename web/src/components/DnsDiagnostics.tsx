import { useRef, useState } from 'react';
import {
  Activity, AlertCircle, ArrowRight, CheckCircle2, ChevronDown, Clock3, Download, FileJson,
  FileUp, Info, Play, RefreshCw, Search, Server, Settings2, ShieldCheck, Split,
} from 'lucide-react';
import * as api from '../api';
import { DIAGNOSTIC_LIMIT, DNS_STATUS_LABELS, makeDiagnosticReport, parseDiagnosticFile } from '../diagnostics';
import type { HostSnapshot } from '../diagnostics';
import './DnsDiagnostics.css';

const COUNTER_LABELS: Record<string, string> = {
  'total.num.queries': '递归入口查询', 'total.num.cachehits': '缓存命中',
  'total.num.cachemiss': '缓存未命中', 'total.num.prefetch': '预取',
  'total.num.expired': '过期缓存应答', 'total.requestlist.exceeded': '递归队列丢弃',
  'time.up': '运行时间增量（秒）',
};

const MODES = [
  { key: 'query', label: '域名查询', icon: Search, action: '查询域名', description: '检查一个域名的解析记录、TTL 和响应时间。' },
  { key: 'compare', label: '链路对照', icon: Split, action: '开始对照', description: '使用同一个域名，比较所选解析路径的返回结果。' },
  { key: 'health', label: '健康检查', icon: ShieldCheck, action: '开始检查', description: '快速检查 CN / 非 CN 两个样本域名能否取得 A 记录。' },
] as const;
type Mode = typeof MODES[number]['key'];

function Result({ label, result, initiallyOpen }: {
  label: string; result: api.DnsTestResult; initiallyOpen: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  const status = result.status ? DNS_STATUS_LABELS[result.status] : result.available ? '已解析' : '探测失败';
  const tone = result.available ? 'success' : ['no_records', 'incomplete'].includes(result.status ?? '') ? 'warning' : 'error';
  const StatusIcon = result.available ? CheckCircle2 : AlertCircle;
  return (
    <details className={`dns-result-card is-${tone}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="dns-result-summary">
        <span className="dns-result-icon"><StatusIcon size={17} /></span>
        <span className="dns-result-identity"><strong>{label}</strong><small>{result.domain}</small></span>
        <span className={`badge ${result.available ? 'badge-green' : tone === 'warning' ? 'badge-amber' : 'badge-red'}`}>{status}</span>
        <span className="dns-result-latency">{result.elapsed_ms ?? '—'}<small>ms</small></span>
        <ChevronDown size={15} className="dns-disclosure-icon" />
      </summary>
      <div className="dns-result-detail">
        <div className="dns-result-meta">
          <span><Server size={13} /> {result.server}:{result.port}</span>
          <span>{result.record_type}</span>
          {result.rcode && <span>{result.rcode}</span>}
          <span>{result.transport?.toUpperCase()}{result.tcp_fallback ? ' · UDP 截断后回退' : ''}</span>
        </div>
        {result.error && <div className="dns-result-error">{result.error}</div>}
        {result.status === 'no_records' && <p className="dns-note">应答中没有该域名或其 CNAME 链上的目标类型记录。</p>}
        {result.answers.length ? <div className="dns-records">
          <div className="dns-record-heading"><span>类型 / 解析记录</span><span>TTL</span></div>
          {result.answers.map((answer, index) => (
            <div className="dns-record-row" key={index}>
              <span className="dns-record-type">{answer.type}</span>
              <div className="dns-record-value"><small>{answer.name}</small><code>{answer.value}</code></div>
              <span className="dns-record-ttl">{answer.ttl}<small>s</small></span>
            </div>
          ))}
        </div> : <p className="dns-note">无完整应答记录</p>}
      </div>
    </details>
  );
}

function SnapshotStats({ snapshot }: { snapshot: HostSnapshot }) {
  const statValue = (key: string) => {
    const value = snapshot.stats_after[key];
    return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString() : '未采集';
  };
  const errors = [snapshot.stats_before.error, snapshot.stats_after.error]
    .filter((value): value is string => typeof value === 'string');
  return (
    <details className="dns-host-stats" open>
      <summary><span><Server size={15} /> 递归统计 <small>{snapshot.container}</small></span><ChevronDown size={15} className="dns-disclosure-icon" /></summary>
      <p className="dns-note">
        采集时长 {snapshot.elapsed_seconds ?? '未知'} 秒。计数包含同期其他设备流量，内部队列丢弃不等于终端解析失败。
      </p>
      {snapshot.observed_counters_reset && <div className="overview-inline-warning">检测到计数器重置，本次增量不可比较。</div>}
      {errors.length > 0 && <div className="overview-inline-warning">部分统计未采集：{[...new Set(errors)].join('；')}</div>}
      <dl className="dns-stat-grid">
        {Object.entries(COUNTER_LABELS).map(([key, label]) => (
          <div key={key}><dt>{label}</dt><dd>{snapshot.observed_counters_reset ? '不可比较'
            : snapshot.stats_delta[key]?.toLocaleString(undefined, { maximumFractionDigits: 2 }) ?? '未采集'}</dd></div>
        ))}
        <div><dt>结束时队列总数</dt><dd>{statValue('total.requestlist.current.all')}</dd></div>
        <div><dt>结束时用户队列</dt><dd>{statValue('total.requestlist.current.user')}</dd></div>
      </dl>
      <p className="dns-note">运行配置指纹：{snapshot.production_configs_unchanged === null ? '无法核验'
        : snapshot.production_configs_unchanged ? '采样前后指纹一致' : '采样前后指纹有变化'}。文件指纹不能证明配置已被进程加载。</p>
    </details>
  );
}

export default function DnsDiagnostics() {
  const [mode, setMode] = useState<Mode>('query');
  const [domain, setDomain] = useState('www.baidu.com');
  const [recordType, setRecordType] = useState<api.DnsTestResult['record_type']>('A');
  const [server, setServer] = useState('paopaodns');
  const [port, setPort] = useState('53');
  const [transport, setTransport] = useState<'udp' | 'tcp'>('udp');
  const [targets, setTargets] = useState([
    { label: '分流入口', port: '53', server: '', enabled: true },
    { label: '原生递归', port: '5301', server: '', enabled: true },
    { label: '加密转发路径', port: '5304', server: '', enabled: true },
  ]);
  const [report, setReport] = useState<api.DiagnosticReport | null>(null);
  const [snapshot, setSnapshot] = useState<HostSnapshot | null>(null);
  const [original, setOriginal] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [source, setSource] = useState<'live' | 'import'>('live');
  const fileInput = useRef<HTMLInputElement>(null);
  const activeMode = MODES.find((item) => item.key === mode)!;
  const successCount = report?.tests.filter((test) => test.result.available).length ?? 0;
  const fallbackCount = report?.tests.filter((test) => test.result.tcp_fallback).length ?? 0;

  const begin = (action: string) => {
    setBusy(action); setError('');
  };
  const parsePort = (value: string) => {
    if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535) throw new Error('端口必须是 1–65535 的整数');
    return Number(value);
  };
  const run = async (action: Mode) => {
    begin(action);
    try {
      const dnsServer = server.trim() || 'paopaodns';
      let next: api.DiagnosticReport;
      if (action === 'compare') {
        const selected = targets.filter((target) => target.enabled);
        if (!selected.length) throw new Error('请至少选择一条解析链路');
        next = await api.compareDns({
          domain, record_type: recordType,
          targets: selected.map((target) => ({ label: target.label, server: target.server.trim() || dnsServer,
            port: parsePort(target.port), transport })),
        });
      } else if (action === 'health') {
        const health = await api.runHealthCheck({ server: dnsServer, port: parsePort(port) });
        next = makeDiagnosticReport(Object.entries(health.tests).flatMap(([key, item]) => item.result
          ? [{ label: key === 'cn' ? 'CN 域名 / A' : '非 CN 域名 / A', result: item.result }] : []));
      } else {
        const result = await api.runDnsTest({ domain, record_type: recordType,
          server: dnsServer, port: parsePort(port), transport });
        next = makeDiagnosticReport([{ label: '单次查询', result }]);
      }
      setReport(next); setSnapshot(null); setOriginal(next); setSource('live');
    } catch (err) {
      setError(err instanceof Error ? err.message : '诊断失败');
    } finally { setBusy(null); }
  };

  const importFile = async (file: File) => {
    begin('import');
    try {
      if (file.size > DIAGNOSTIC_LIMIT) throw new Error('报告不能超过 2 MB');
      const imported = parseDiagnosticFile(await file.text());
      setReport(imported.report); setSnapshot(imported.snapshot); setOriginal(imported.original); setSource('import');
    } catch (err) {
      setError(err instanceof Error ? err.message : '报告读取失败');
    } finally { setBusy(null); }
  };

  const exportReport = () => {
    const blob = new Blob([JSON.stringify(original, null, 2) + '\n'], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `paopaodns-diagnostic-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  return (
    <section className="card dns-workspace" aria-labelledby="dns-title">
      <div className="dns-workspace-header">
        <div className="dns-workspace-heading">
          <span className="dns-heading-icon"><Activity size={21} /></span>
          <div><h3 id="dns-title">DNS 诊断</h3><p>从解析记录到链路对照，定位查询问题</p></div>
        </div>
        <div className="dns-file-actions">
          <button className="btn btn-secondary btn-sm" disabled={busy !== null} onClick={() => fileInput.current?.click()}><FileUp size={14} /> 导入报告</button>
          <button className="btn btn-secondary btn-sm" disabled={!report || busy !== null} onClick={exportReport}><Download size={14} /> 导出 JSON</button>
          <input ref={fileInput} className="dns-file-input" type="file" accept=".json,application/json" aria-label="选择诊断 JSON 文件" onChange={(e) => {
            const file = e.target.files?.[0]; e.target.value = ''; if (file) void importFile(file);
          }} />
        </div>
      </div>
      <div className="dns-workspace-body">
        <form className="dns-query-form" onSubmit={(event) => { event.preventDefault(); if (!busy) void run(mode); }}>
          <fieldset disabled={busy !== null}>
            <legend className="visually-hidden">DNS 探测设置</legend>
            <div className="dns-mode-switch" role="group" aria-label="诊断模式">
              {MODES.map((item) => <button key={item.key} type="button" aria-pressed={mode === item.key}
                onClick={() => { setMode(item.key); setError(''); }}><item.icon size={15} />{item.label}</button>)}
            </div>
            <p className="dns-mode-description">{activeMode.description}</p>
            {mode === 'health' ? <div className="dns-health-targets">
              <span><span className="dns-category-label">CN</span><code>www.baidu.com</code></span>
              <span><span className="dns-category-label">非 CN</span><code>www.google.com</code></span>
              <small>固定检查 A 记录 · UDP 查询，截断时回退 TCP</small>
            </div> : <div className="dns-domain-fields">
              <div className="form-group"><label className="form-label" htmlFor="dns-domain">查询域名</label>
                <input id="dns-domain" className="form-input" value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="例如 www.example.com" autoCapitalize="none" autoCorrect="off" spellCheck={false} /></div>
              <div className="form-group"><label className="form-label" htmlFor="dns-type">记录类型</label>
                <select id="dns-type" className="form-select" value={recordType} onChange={(e) => setRecordType(e.target.value as typeof recordType)}>
                  <option>A</option><option>AAAA</option><option>CNAME</option>
                </select></div>
            </div>}
            <div className={`dns-endpoint-fields ${mode === 'compare' ? 'is-compare' : ''}`}>
              <div className="form-group"><label className="form-label" htmlFor="dns-server">{mode === 'compare' ? '默认 DNS 服务' : 'DNS 服务'}</label>
                <input id="dns-server" className="form-input" value={server} onChange={(e) => setServer(e.target.value)} placeholder="paopaodns" autoCapitalize="none" spellCheck={false} /></div>
              {mode !== 'compare' && <div className="form-group"><label className="form-label" htmlFor="dns-port">端口</label>
                <input id="dns-port" className="form-input" type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} /></div>}
            </div>
            {mode === 'compare' && <div className="dns-target-list">
              <div className="dns-target-caption"><span>选择解析路径</span><span>服务地址 / 端口</span></div>
              {targets.map((target, index) => <div className={`dns-target-row ${target.enabled ? 'is-selected' : ''}`} key={target.label}>
                <label><input type="checkbox" checked={target.enabled} onChange={(e) => setTargets(targets.map((t, i) => i === index ? { ...t, enabled: e.target.checked } : t))} /><span>{target.label}</span></label>
                <div className="dns-target-endpoint">
                  <input className="form-input" disabled={!target.enabled} aria-label={`${target.label} DNS 服务`} value={target.server} placeholder={server || 'paopaodns'} onChange={(e) => setTargets(targets.map((t, i) => i === index ? { ...t, server: e.target.value } : t))} />
                  <input className="form-input" disabled={!target.enabled} aria-label={`${target.label}端口`} type="number" min={1} max={65535} value={target.port} onChange={(e) => setTargets(targets.map((t, i) => i === index ? { ...t, port: e.target.value } : t))} />
                </div>
              </div>)}
            </div>}
            {mode !== 'health' && <details className="dns-options" key={mode}>
              <summary><span><Settings2 size={14} /> 查询选项 <small>{transport === 'udp' ? 'UDP · 自动回退 TCP' : '直接使用 TCP'}</small></span><ChevronDown size={14} className="dns-disclosure-icon" /></summary>
              <div className="form-group"><label className="form-label" htmlFor="dns-transport">传输协议</label>
                <select id="dns-transport" className="form-select" value={transport} onChange={(e) => setTransport(e.target.value as typeof transport)}>
                  <option value="udp">UDP，截断时自动回退 TCP</option><option value="tcp">TCP</option>
                </select></div>
            </details>}
            <button className="btn btn-primary dns-run-button" type="submit">
              {busy && busy !== 'import' ? <RefreshCw size={16} className="dns-spinner" /> : <Play size={15} />}
              {busy && busy !== 'import' ? '正在探测…' : activeMode.action}
              {!busy && mode === 'compare' && <small>{targets.filter((target) => target.enabled).length} 条链路</small>}
              {!busy && <ArrowRight size={15} />}
            </button>
            <p className="dns-note dns-network-note"><Info size={13} /><span>{mode === 'compare'
              ? '路径地址留空时沿用默认服务；5304 包含缓存，耗时不等于远端上游耗时。'
              : '查询从 WebUI 所在网络发出，请填写它能访问的 DNS 地址。'}</span></p>
          </fieldset>
        </form>
        <div className="dns-results-area" aria-busy={busy !== null}>
          <div className="dns-results-heading"><h4>诊断结果</h4><span className={`dns-source-label ${source === 'import' && report ? 'is-imported' : ''}`}>
            {report ? source === 'import' ? <><FileJson size={12} />已导入 · 离线记录</> : <><Clock3 size={12} />按需探测记录</> : '等待执行'}</span></div>
          <div role="status" aria-live="polite">
            {busy && <div className="dns-progress"><RefreshCw size={14} className="dns-spinner" /> {busy === 'import' ? '正在读取本地报告…' : '正在执行 DNS 探测，完成后更新下方结果…'}</div>}
            {error && <div className="dns-error-banner" role="alert"><AlertCircle size={16} /><span>{error}{report && <small>已保留上一次报告。</small>}</span></div>}
            {report && <div className="dns-report-overview">
              <div className="dns-report-score"><strong>{successCount}<small> / {report.tests.length}</small></strong><span>取得目标记录</span></div>
              <div className="dns-report-facts"><span className={successCount === report.tests.length ? 'is-success' : 'is-attention'}>
                {successCount === report.tests.length ? <CheckCircle2 size={14} /> : <AlertCircle size={14} />}
                {successCount === report.tests.length ? '全部样本已解析' : `${report.tests.length - successCount} 项需检查`}</span>
                {fallbackCount > 0 && <span>{fallbackCount} 项使用 TCP 回退</span>}
                <time dateTime={report.started_utc}>{new Date(report.started_utc).toLocaleString()}</time>
              </div>
            </div>}
          </div>
          {report ? <>
            {source === 'import' && <p className="dns-offline-note"><FileJson size={14} />导入结果仅反映采集时的状态。</p>}
            {snapshot && <SnapshotStats key={report.started_utc} snapshot={snapshot} />}
            <div className="dns-results-list">{report.tests.map((test, index) => <Result key={`${report.started_utc}-${index}`}
              {...test} initiallyOpen={report.tests.length === 1 || !test.result.available} />)}</div>
          </> : !busy && <div className="dns-empty-state">
            <span className="dns-empty-icon"><Search size={25} /></span>
            <h4>从一次查询开始</h4><p>输入域名，查看解析记录与响应时间。<br />需要定位差异时，切换到链路对照。</p>
            <span><FileJson size={13} /> 也可导入已有 JSON 报告或宿主机快照</span>
          </div>}
        </div>
      </div>
      <details className="dns-help">
        <summary><Info size={14} />关于探测结果与报告<ChevronDown size={14} className="dns-disclosure-icon" /></summary>
        <div><p>普通 DNS 探测不会清空缓存，可能预热少量缓存。TTL 为 0 本身不判失败；解析成功不代表答案新鲜或网站可访问。</p>
          <p>导入支持 WebUI 报告及 paopaodns-health-snapshot.py 的 JSON，最大 2 MB，仅在浏览器内读取。报告包含域名、IP 等信息，分享前请检查内容。</p>
          <p>内部端口是否可达取决于功能开关、监听和容器网络。对照失败本身不代表 DNS 核心故障。</p></div>
      </details>
    </section>
  );
}
