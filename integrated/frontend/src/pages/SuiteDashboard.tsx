import { Activity, ArrowUpRight, Copy, Network, Radio, Shield } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useSuite } from '../suiteContext';
import type { ProbeSample } from '../suiteContext';
import { filterStatus, useFilterView } from '../filterContext';
import { filterPreviewHref } from '../filterApi';
import { useGateway } from '../gatewayContext';
import { when } from '../suiteApi';
import { Notice, Offline } from '../components/SuiteUI';
import { componentSummary, CORE } from '../runtimePresentation';
import { dnsAddress, entryListeners } from '../dnsListeners';
import { processFingerprint } from '../queryEvidence';
import { copyTextFromPage } from '../copyText';
import QueryAnalytics from '../components/QueryAnalytics';
import { isActiveTask, taskLabel, TASK_NAMES, useMaintenance } from '../maintenanceContext';
import './SuitePage.css';
import './HomeWorkflow.css';

const severity = { error: 0, warning: 1, info: 2 } as const;
type HomeAlert = { id: string; tone: keyof typeof severity; content: ReactNode };

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

function DnsEntryAddress({ addresses }: { addresses: string[] }) {
  const [feedback, setFeedback] = useState<'copied' | 'failed' | null>(null);
  useEffect(() => {
    if (feedback !== 'copied') return;
    const timer = window.setTimeout(() => setFeedback(null), 2000);
    return () => window.clearTimeout(timer);
  }, [feedback]);
  if (!addresses.length) return <strong className="suite-home-address">监听待确认</strong>;
  return <div className="suite-home-address-row">
    <strong className="suite-home-address"><code>{addresses.join(' · ')}</code></strong>
    <button type="button" className="btn btn-secondary btn-sm" aria-label="复制监听地址"
      onClick={async () => setFeedback(await copyTextFromPage(addresses.join('\n')) ? 'copied' : 'failed')}>
      <Copy size={14} aria-hidden="true" /><span aria-live="polite">{feedback === 'copied' ? '已复制' : '复制'}</span>
    </button>
    {feedback === 'failed' && <p role="status">自动复制失败，可选中监听地址手动复制。</p>}
  </div>;
}

export default function SuiteDashboard() {
  const { caps, runtime, paused, runtimeStale, runtimeError, error,
    probe: check, probeChecking: checking, probeError: checkError, runProbe } = useSuite();
  const filter = useFilterView();
  const { config: gateway } = useGateway();
  const { tasks, submission, readError } = useMaintenance();
  const result = check?.result;
  const probeSamples = Object.values(result?.tests ?? {});
  const probeTimes = probeSamples.map(sample => Date.parse(sample.result.checked_at ?? '') / 1000).filter(Number.isFinite);
  const probeTime = probeTimes.length ? Math.max(...probeTimes) : undefined;
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
  if (!caps) return <Offline />;
  const components = runtime?.components ?? {};
  const auxiliary = Object.keys(components).filter((name) => !CORE.includes(name));
  const problems = auxiliary.filter((name) => ['warning', 'error'].includes(componentSummary(name, components[name]).tone));
  const endpoints = entryListeners(runtime);
  const addresses = [...new Set(endpoints.map((endpoint) => dnsAddress(endpoint.address, endpoint.port)))];
  const historical = paused || runtimeStale;
  const process = error ? '面板连接中断' : historical ? '显示历史快照' : runtime?.core_healthy ? 'DNS 进程运行中' : runtime ? 'DNS 进程需检查' : '等待运行状态';
  const alerts: HomeAlert[] = [];
  if (!activeConfig && (runtime?.error || runtimeError)) {
    alerts.push({ id: 'core', tone: 'error', content: runtime?.error || runtimeError });
  }
  else if (!activeConfig && runtime && !runtime.core_healthy) alerts.push({
    id: 'core', tone: 'warning', content: <>核心组件需检查。{caps.permissions.maintain && <a href="#/system?tab=logs&component=bootstrap">查看启动日志</a>}</>,
  });
  if (problems.length) alerts.push({
    id: 'auxiliary', tone: 'warning', content: <>辅助服务需检查。{problems.map((name, i) => <span key={name}>
      {i > 0 && '、'}{caps.permissions.maintain ? <a href={`#/system?tab=logs&component=${encodeURIComponent(name)}`}>{name}</a> : name}
    </span>)}</>,
  });
  if (shownSubmission) {
    const phase = shownSubmission.phase;
    alerts.push({ id: 'submission', tone: phase === 'failed' ? 'error' : phase === 'uncertain' ? 'warning' : 'info',
      content: <a href="#/system?tab=tasks">{TASK_NAMES[shownSubmission.action]} · {phase === 'sending' ? '正在提交' : phase === 'failed' ? '提交失败' : '正在核对受理记录'}</a> });
  } else if (latest?.state === 'failed') alerts.push({
    id: 'task', tone: 'warning', content: <a href="#/system?tab=tasks">{taskLabel(latest)} · 查看任务详情</a>,
  });
  const [firstAlert, ...otherAlerts] = alerts.sort((a, b) => severity[a.tone] - severity[b.tone]);
  return <div className="suite-page suite-home">
    <section className="card suite-home-health" data-healthy={!!runtime?.core_healthy && !activeConfig && !historical && !error} aria-label="DNS 状态与待处理操作">
      <div className="suite-home-health-main">
        <p className="suite-home-instance"><Network size={15} />{caps.name}</p>
        <div role="status">
          <h2><Activity size={21} aria-hidden="true" />{activeConfig ? `${error || readError ? '上次看到 · ' : ''}${taskLabel(activeConfig)}` : process}</h2>
          <p>{activeConfig ? `任务更新于 ${when(activeConfig.updated)}`
            : runtime ? `进程状态采样于 ${when(runtime.sampled_at)}` : runtimeError ? '运行状态暂不可用' : '正在连接 DNS 实例'}</p>
        </div>
        <div className="suite-home-actions">{activeConfig ? <a className="btn btn-primary" href="#/system?tab=tasks">查看任务进度</a> : caps.permissions.diagnose && <button className="btn btn-primary" disabled={checking || !!error} onClick={() => void runProbe()}>
          {checking ? '正在测试解析…' : '测试本机解析'}</button>}
          <a href="#/diagnostics?tab=query&help=device">检查设备接入<ArrowUpRight size={13} /></a>
          <a href="#/system?tab=status">查看运行状态<ArrowUpRight size={13} /></a></div>
        <div className="suite-probe-region" aria-live="polite" aria-atomic="true">
          {checking && <p>正在向本机 DNS 查询两个样本域名…</p>}
          {checkError && <div className="suite-probe-result is-failed">
            <strong>未能完成本机解析测试</strong>
            <p>请重新测试，或在「查询与诊断」中单独查询域名。</p>
            <details><summary>请求详情</summary><p>{checkError}</p></details>
          </div>}
          {result && <div className={`suite-probe-result ${probeHistorical ? 'is-historical' : result.pass ? 'is-passed' : 'is-failed'}`}>
            <strong>{probeHistorical && '历史样本 · '}{result.pass ? '本机解析测试通过' : '部分或全部样本解析失败'}</strong>
            <small>{dnsAddress(result.server, result.port)} · {probeSamples.filter(sample => sample.resolved).length} / {probeSamples.length} 个域名取得 A 记录</small>
            <ul>{probeSamples.map(sample => <li key={sample.domain}>
              <code>{sample.domain}</code><span>{probeResultText(sample)}{typeof sample.result.elapsed_ms === 'number' ? ` · ${sample.result.elapsed_ms} ms` : ''}</span>
              {!sample.resolved && caps.permissions.diagnose && <a href={`#/diagnostics?tab=query&domain=${encodeURIComponent(sample.domain)}`}>
                主动查询<ArrowUpRight size={13} aria-hidden="true" />
              </a>}
            </li>)}</ul>
            <small>从本机服务发起 · {probeTime !== undefined ? when(probeTime) : '查询时间未返回'}</small>
          </div>}
        </div>
      </div>
      <div className="suite-home-followup suite-home-entry">
        <div><h3><Radio size={16} />{caps.deployment === 'container' ? '容器内 DNS 入口' : 'DNS 监听入口'}</h3>
          <DnsEntryAddress key={addresses.join('|')} addresses={addresses} />
          {historical && <small>上次采样</small>}</div>
        {caps.features.dns_filter && caps.permissions.configure && <div><h3><Shield size={16} />广告过滤</h3>
          <a href="#/filter">{(filter.error || paused ? '上次读取 · ' : '') + filterStatus(filter.state)}</a>
          {filter.state?.draft && <p><a href={filterPreviewHref(filter.state.draft)}>订阅待复核 · {filter.state.draft.name}</a></p>}</div>}
      </div>
    </section>
    {firstAlert && <div className="suite-home-alerts">
      <Notice tone={firstAlert.tone}>{firstAlert.content}</Notice>
      {otherAlerts.length > 0 && <details className="suite-home-alerts-more">
        <summary>另有 {otherAlerts.length} 项待处理提醒</summary>
        <div>{otherAlerts.map(alert => <Notice key={alert.id} tone={alert.tone}>{alert.content}</Notice>)}</div>
      </details>}
    </div>}
    {caps.features.query_recording && caps.permissions.configure
      && <QueryAnalytics key={caps.instance_id} instance={caps.instance_id} paused={paused} />}
    {gateway?.enabled && <a className="suite-gateway-link" href="#/diagnostics?tab=gateway"><strong>{gateway.name ?? 'LovestGateway'}</strong><span>查看网关观测 →</span></a>}
  </div>;
}
