import { Network } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useSuite } from '../suiteContext';
import { filterStatus, useFilterView } from '../filterContext';
import { useGateway } from '../gatewayContext';
import { errorText, suiteFetch, when } from '../suiteApi';
import { Notice, Offline } from '../components/SuiteUI';
import { componentSummary, CORE } from '../runtimePresentation';
import { dnsAddress, entryListeners } from '../dnsListeners';
import { processFingerprint } from '../queryEvidence';
import QueryAnalytics from '../components/QueryAnalytics';
import { isActiveTask, taskLabel, TASK_NAMES, useMaintenance } from '../maintenanceContext';
import './SuitePage.css';
import './HomeWorkflow.css';

const PROBE_FRESH_MS = 5 * 60_000;
type ProbeSample = {
  domain: string; resolved: boolean;
  result: { responded?: boolean; status?: string; rcode?: string; elapsed_ms?: number; checked_at?: string };
};
type ProbeResult = { pass: boolean; tests: Record<string, ProbeSample>; server: string; port: number };
type ProbeObservation = { result: ProbeResult; process: string | null; expired?: boolean };

function probeResultText(sample: ProbeSample) {
  const { responded, status, rcode } = sample.result;
  if (sample.resolved) return '取得 A 记录';
  if (status === 'timeout') return '等待 DNS 应答超时';
  if (rcode === 'NXDOMAIN') return '域名不存在（NXDOMAIN）';
  if (rcode === 'SERVFAIL') return '服务返回解析失败（SERVFAIL）';
  if (rcode === 'REFUSED') return '服务拒绝解析（REFUSED）';
  if (responded) return rcode && rcode !== 'NOERROR' ? `DNS 返回 ${rcode}` : 'DNS 已应答，未取得 A 记录';
  return '未收到有效 DNS 应答';
}

export default function SuiteDashboard() {
  const { caps, runtime, paused, runtimeStale, runtimeError, error } = useSuite();
  const filter = useFilterView();
  const { config: gateway } = useGateway();
  const { tasks, submission, readError } = useMaintenance();
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<ProbeObservation | null>(null);
  const [checkError, setCheckError] = useState('');
  const result = check?.result;
  const probeSamples = Object.values(result?.tests ?? {});
  const probeTimes = probeSamples.map(sample => Date.parse(sample.result.checked_at ?? '') / 1000).filter(Number.isFinite);
  const probeTime = probeTimes.length ? Math.max(...probeTimes) : undefined;
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!check || check.expired) return;
    const timer = setTimeout(() => setCheck(current => current === check ? { ...check, expired: true } : current), PROBE_FRESH_MS);
    return () => clearTimeout(timer);
  }, [check]);
  const active = tasks.find(isActiveTask);
  const latest = active ?? tasks[0];
  const shownSubmission = !active && submission && (submission.phase !== 'failed' || !latest) ? submission : null;
  const configTasks = tasks.filter(task => task.action !== 'save_redis_snapshot');
  const activeConfig = configTasks.find(isActiveTask);
  const latestConfigUpdate = Math.max(0, ...configTasks.map(task => task.updated));
  const currentProcess = processFingerprint(runtime);
  // Check task and sample times on the service clock, even if the browser clock differs.
  const probeHistorical = !!check && (check.expired || checking || !!checkError || !!activeConfig
    || !!readError || !!error || paused || runtimeStale || check.process !== currentProcess
    || (latestConfigUpdate > 0 && (probeTimes.length !== probeSamples.length || Math.min(...probeTimes) <= latestConfigUpdate)));
  const checkDns = async () => {
    if (!caps || controller.current || activeConfig) return;
    const abort = new AbortController();
    controller.current = abort;
    setChecking(true); setCheckError('');
    try {
      const params = new URLSearchParams({ server: caps.dns_defaults.server, port: String(caps.dns_defaults.port) });
      const result = await suiteFetch<ProbeResult>(`/api/v1/health-check?${params}`, { signal: abort.signal });
      const samples = Object.values(result.tests ?? {});
      if (typeof result.pass !== 'boolean' || samples.length === 0 || samples.some(sample => typeof sample?.domain !== 'string'
        || typeof sample.resolved !== 'boolean' || !sample.result || typeof sample.result !== 'object')) throw new Error('服务没有返回有效的域名测试结果');
      if (!abort.signal.aborted) setCheck({ result, process: currentProcess });
    } catch (error) {
      if (!abort.signal.aborted) setCheckError(errorText(error));
    } finally {
      if (controller.current === abort) controller.current = null;
      if (!abort.signal.aborted) setChecking(false);
    }
  };
  if (!caps) return <Offline />;
  const components = runtime?.components ?? {};
  const auxiliary = Object.keys(components).filter((name) => !CORE.includes(name));
  const problems = auxiliary.filter((name) => ['warning', 'error'].includes(componentSummary(name, components[name]).tone));
  const endpoints = entryListeners(runtime);
  const addresses = [...new Set(endpoints.map((endpoint) => dnsAddress(endpoint.address, endpoint.port)))];
  const historical = paused || runtimeStale;
  const process = error ? '面板连接中断' : historical ? '显示历史快照' : runtime?.core_healthy ? 'DNS 进程运行中' : runtime ? 'DNS 进程需检查' : '等待运行状态';
  return <div className="suite-page suite-home">
    <section className="card suite-home-health" aria-label="DNS 状态与待处理操作">
      <div className="suite-home-health-main">
        <p className="suite-home-instance"><Network size={15} />{caps.name}</p>
        <div role="status">
          <h2>{activeConfig ? `${error || readError ? '上次看到 · ' : ''}${taskLabel(activeConfig)}` : process}</h2>
          <p>{activeConfig ? `任务更新于 ${when(activeConfig.updated)}`
            : runtime ? `进程状态采样于 ${when(runtime.sampled_at)}` : runtimeError || '正在连接 DNS 实例'}</p>
        </div>
        <div className="suite-home-actions">{activeConfig ? <a className="btn btn-primary" href="#/system?tab=tasks">查看任务进度</a> : caps.permissions.diagnose && <button className="btn btn-primary" disabled={checking || !!error} onClick={() => void checkDns()}>
          {checking ? '正在测试解析…' : '测试本机解析'}</button>}
          <a href="#/diagnostics?tab=query&help=device">检查设备接入</a>
          <a href="#/system?tab=status">查看运行状态</a></div>
        <div className="suite-probe-region" aria-live="polite" aria-atomic="true">
          {checking && <p>正在向本机 DNS 查询两个样本域名…</p>}
          {checkError && <div className="suite-probe-result is-failed">
            <strong>未能完成本机解析测试</strong>
            <p>请重新测试，或在「查询与诊断」中单独查询域名。</p>
            <details><summary>请求详情</summary><p>{checkError}</p></details>
          </div>}
          {!checking && !checkError && !result && !activeConfig && caps.permissions.diagnose && <p className="suite-note">本次会话尚未测试本机解析。</p>}
          {result && <div className={`suite-probe-result ${probeHistorical ? 'is-historical' : result.pass ? 'is-passed' : 'is-failed'}`}>
            <strong>{probeHistorical && '历史样本 · '}{result.pass ? '本机解析测试通过' : '部分或全部样本解析失败'}</strong>
            <small>{dnsAddress(result.server, result.port)} · {probeSamples.filter(sample => sample.resolved).length} / {probeSamples.length} 个域名取得 A 记录</small>
            <ul>{probeSamples.map(sample => <li key={sample.domain}>
              <code>{sample.domain}</code><span>{probeResultText(sample)}{typeof sample.result.elapsed_ms === 'number' ? ` · ${sample.result.elapsed_ms} ms` : ''}</span>
            </li>)}</ul>
            <small>从本机服务发起 · {probeTime !== undefined ? when(probeTime) : '查询时间未返回'}</small>
          </div>}
        </div>
      </div>
      <div className="suite-home-followup suite-home-entry">
        <div><h3>{caps.deployment === 'container' ? '容器内 DNS 入口' : 'DNS 监听入口'}</h3>
          <strong className="suite-home-address">{addresses.join(' · ') || '监听待确认'}</strong>
          {historical && <small>上次采样</small>}</div>
        {caps.features.dns_filter && caps.permissions.configure && <div><h3>广告过滤</h3>
          <a href="#/filter">{(filter.error || paused ? '上次读取 · ' : '') + filterStatus(filter.state)}</a>
          {filter.state?.draft && <p><a href="#/filter?tab=rules">订阅待复核</a></p>}</div>}
      </div>
    </section>
    {!activeConfig && (runtime?.error ? <Notice tone="error">{runtime.error}</Notice> : runtime && !runtime.core_healthy ? <Notice tone="warning">核心组件需检查。<a href="#/system?component=bootstrap">查看启动日志</a></Notice> : null)}
    {!!problems.length && <Notice tone="warning">辅助服务需检查。{problems.map((name, i) => <span key={name}>{i > 0 && '、'}<a href={`#/system?component=${encodeURIComponent(name)}`}>{name}</a></span>)}</Notice>}
    {shownSubmission && <Notice><a href="#/system?tab=tasks">{TASK_NAMES[shownSubmission.action]} · {shownSubmission.phase === 'sending' ? '正在提交' : shownSubmission.phase === 'failed' ? '提交失败' : '正在核对受理记录'}</a></Notice>}
    {!shownSubmission && latest?.state === 'failed' && <Notice tone="warning"><a href="#/system?tab=tasks">{taskLabel(latest)} · 查看任务详情</a></Notice>}
    {caps.features.query_recording && caps.permissions.configure
      && <QueryAnalytics key={caps.instance_id} instance={caps.instance_id} paused={paused} />}
    {gateway?.enabled && <a className="suite-gateway-link" href="#/diagnostics?tab=gateway"><strong>{gateway.name ?? 'LovestGateway'}</strong><span>查看网关观测 →</span></a>}
  </div>;
}
