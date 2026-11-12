import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSuite } from '../suiteContext';
import { dirtyFileDrafts, fileDraft, subscribeFileDraft } from '../fileDrafts';
import { suiteFetch, errorText } from '../suiteApi';
import type { RuntimeSnapshot, SuiteDnsResult } from '../suiteApi';
import { configurationFingerprint, processFingerprint } from '../queryEvidence';
import type { ConfigurationVersions } from '../queryEvidence';
import { dnsAddress } from '../dnsListeners';
import { runtimeLifetimeMs } from '../runtimeFreshness';

const subscribeDrafts = (listener: () => void) => subscribeFileDraft('*', listener);
interface Evidence {
  result: SuiteDnsResult; versions: ConfigurationVersions; process: string | null;
  consistent: boolean; generation: number; instance: string;
}
export default function ConfigVerification({ file }: { file?: string }) {
  const { caps, runtime, paused, runtimeStale, error: connectionError } = useSuite();
  const [domain, setDomain] = useState('');
  const [record, setRecord] = useState('A');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [currentConfig, setCurrentConfig] = useState<{ fingerprint: string; checkedAt: number } | null>(null);
  const [checkError, setCheckError] = useState('');
  const [generation, setGeneration] = useState(0);
  const changes = useRef(0);
  const request = useRef<AbortController | null>(null);
  const unsaved = useSyncExternalStore(subscribeDrafts, dirtyFileDrafts);
  useEffect(() => {
    const changed = () => { changes.current++; setGeneration(changes.current); };
    window.addEventListener('lovestdns-config-changed', changed);
    return () => { request.current?.abort(); window.removeEventListener('lovestdns-config-changed', changed); };
  }, []);
  useEffect(() => {
    if (!evidence) return;
    const abort = new AbortController();
    let reading = false;
    const check = async () => {
      if (document.hidden || reading) return;
      reading = true;
      try {
        const versions = await suiteFetch<ConfigurationVersions>('/api/v1/config/state', { signal: abort.signal });
        if (!abort.signal.aborted) {
          setCurrentConfig({ fingerprint: configurationFingerprint(versions), checkedAt: performance.now() });
          setCheckError('');
        }
      } catch (e) {
        if (!abort.signal.aborted) { setCurrentConfig(null); setCheckError(errorText(e)); }
      } finally { reading = false; }
    };
    const timer = setInterval(() => void check(), 15000);
    document.addEventListener('visibilitychange', check);
    return () => { abort.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', check); };
  }, [evidence]);
  useEffect(() => {
    if (!currentConfig) return;
    const timer = setTimeout(() => setCurrentConfig(null), Math.max(0, 45000 - (performance.now() - currentConfig.checkedAt)));
    return () => clearTimeout(timer);
  }, [currentConfig]);
  const run = async () => {
    if (!caps?.permissions.diagnose || !caps.permissions.configure || busy || dirtyFileDrafts()) return;
    const abort = new AbortController(); request.current = abort;
    setBusy(true); setError('');
    const startGeneration = changes.current;
    const context = async () => {
      const started = performance.now();
      const result = await Promise.all([
        suiteFetch<ConfigurationVersions>('/api/v1/config/state', { signal: abort.signal }),
        suiteFetch<RuntimeSnapshot>('/api/v1/runtime', { signal: abort.signal }),
      ]);
      runtimeLifetimeMs(result[1], performance.now() - started);
      return result;
    };
    try {
      const [before, beforeRuntime] = await context();
      const result = await suiteFetch<SuiteDnsResult>('/api/v1/dns-test', { signal: abort.signal, method: 'POST',
        body: JSON.stringify({ domain: domain.trim(), record_type: record, ...caps.dns_defaults, transport: 'udp' }) });
      const [after, afterRuntime] = await context();
      if (abort.signal.aborted) return;
      const process = processFingerprint(beforeRuntime);
      setCurrentConfig({ fingerprint: configurationFingerprint(after), checkedAt: performance.now() });
      setCheckError('');
      setEvidence({ result, versions: before, process, generation: startGeneration, instance: caps.instance_id,
        consistent: before.files.length > 0 && (!file || before.files.some((row) => row.filename === file)) && process !== null && process === processFingerprint(afterRuntime)
          && configurationFingerprint(before) === configurationFingerprint(after) && startGeneration === changes.current });
    } catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  const draftChanged = evidence?.versions.files.some(({ filename, revision }) => {
    const draft = fileDraft(filename);
    return draft?.loaded && draft.revision !== revision;
  });
  const historical = evidence && (!evidence.consistent || generation !== evidence.generation || unsaved || draftChanged
    || !currentConfig
    || currentConfig.fingerprint !== configurationFingerprint(evidence.versions)
    || evidence.instance !== caps?.instance_id || paused || runtimeStale || evidence.process !== processFingerprint(runtime));
  const revision = file && evidence?.versions.files.find((row) => row.filename === file)?.revision;
  return <section className="suite-inline-query" aria-label="配置样本查询">
    <h3>样本查询</h3><p className="suite-note">从服务端向默认诊断目标查询；取得记录后，仍需核对预期答案和实际分流。</p>
    <form onSubmit={(event) => { event.preventDefault(); void run(); }}>
      <label>验证域名<input className="form-input" required maxLength={253} value={domain} disabled={busy} onChange={(event) => setDomain(event.target.value)} placeholder="输入需要验证的域名" /></label>
      <label>类型<select className="form-select" value={record} disabled={busy} onChange={(event) => setRecord(event.target.value)}><option>A</option><option>AAAA</option></select></label>
      <button className="btn btn-secondary" disabled={busy || unsaved || !domain.trim() || !caps?.permissions.diagnose || !caps.permissions.configure || !!connectionError}>{busy ? '查询与核对中…' : '验证样本查询'}</button>
    </form>
    {unsaved && <p role="status">先保存或还原草稿，再核对已保存配置。</p>}
    {error && <p role="alert">查询或配置核对未完成：{error}。{evidence && '下方保留上次结果。'}</p>}
    {evidence && <div aria-live="polite"><p><strong>{busy ? '上次查询结果' : historical ? '历史样本 · 当前配置效果待确认' : evidence.result.available ? '本次样本取得目标记录' : '本次样本未取得目标记录'}</strong></p>
      <p>{evidence.result.domain} · {evidence.result.record_type} · {evidence.result.rcode ?? evidence.result.status} · {evidence.result.elapsed_ms ?? '—'} ms</p>
      <p>{dnsAddress(evidence.result.server, evidence.result.port)} · {evidence.result.transport?.toUpperCase()} · {evidence.result.checked_at ? new Date(evidence.result.checked_at).toLocaleString('zh-CN') : '查询时间未返回'}</p>
      {evidence.result.error && <p>{evidence.result.error}</p>}
      {!!evidence.result.answers.length && <ul>{evidence.result.answers.map((answer, index) => <li key={index}>{answer.type} · {answer.value} · TTL {answer.ttl}s</li>)}</ul>}
      {checkError && <p role="status">当前配置核对暂不可用，样本按历史参考显示：{checkError}</p>}
      <details><summary>样本依据</summary><p>{revision ? `${file} · ${revision.slice(0, 12)}` : `${evidence.versions.files.length} 个配置版本已记录`}</p><p>{evidence.consistent ? '查询前后读取的文件版本与核心进程采集标识一致。' : '查询期间配置或进程依据不足，结果只作历史参考。'}本次结果不证明终端接入或具体规则命中。停留本页时每 15 秒核对配置版本。</p></details>
    </div>}
  </section>;
}
