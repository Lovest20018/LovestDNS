import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Clock, Database, FileCheck, RefreshCw } from 'lucide-react';
import { bytes, errorText, suiteFetch, when } from '../suiteApi';
import { useSuiteConfig } from '../suiteContext';
import { dirtyFileDrafts, dirtyFileNames, subscribeFileDraft } from '../fileDrafts';
import { configFileLabel, configFileLink } from '../configFiles';
import { isActiveTask, requestExpired, taskLabel, TASK_NAMES, useMaintenance } from '../maintenanceContext';
import type { ApplyPlan, Capabilities, MaintenanceTask, RuntimeSnapshot } from '../suiteApi';

const WORKER_PHASES: Record<string, string> = {
  result_unconfirmed: '正在核对任务记录', journal_unavailable: '暂时无法读取任务记录', starting: '管理服务正在启动',
};
const subscribeDrafts = (listener: () => void) => subscribeFileDraft('*', listener);

const logLink = (task: MaintenanceTask) => `#/system?component=${task.action === 'save_redis_snapshot' ? 'redis' : task.action === 'apply_custom_mod' ? 'mosdns' : 'bootstrap'}`;

function TaskResult({ task, historical = false, next = '#/diagnostics?tab=query' }: { task: MaintenanceTask; historical?: boolean; next?: string }) {
  return <div className={`suite-task-result is-${historical ? 'historical' : task.state}`}>
    <strong>{historical ? '上次看到 · ' : ''}{taskLabel(task)}</strong>
    <small>{when(task.updated)} · 任务 {task.id.slice(0, 12)}</small>
    {task.result?.message && <p>{task.result.message}</p>}
    {task.result?.error && <p>{task.result.error}</p>}
    {task.result?.rollback && <p>运行配置{task.result.rollback.runtime_restored ? '已恢复' : '恢复待确认'}。DNS 服务{task.result.rollback.service_running ? '已启动' : '需检查'}。{task.result.rollback.error}</p>}
    {task.result?.saved_at && <p>{when(task.result.saved_at)} 保存了 {bytes(task.result.bytes)}</p>}
    {task.state === 'succeeded' && task.action !== 'save_redis_snapshot' && <a href={next} onClick={next.startsWith('#/') ? undefined : (event) => {
      event.preventDefault();
      const field = document.getElementById('config-query-domain');
      field?.scrollIntoView({ block: 'center' });
      field?.focus({ preventScroll: true });
    }}>验证目标域名</a>}
    {['failed', 'interrupted'].includes(task.state) && <div className="suite-local-links"><a href={logLink(task)}>查看相关日志</a><a href="#/config">检查配置</a></div>}
  </div>;
}

export default function SuiteMaintenance({ caps, runtime, mode = 'system', configAction = 'all' }: { caps: Capabilities; runtime: RuntimeSnapshot | null; mode?: 'config' | 'system' | 'cache'; configAction?: 'all' | 'restart' | 'apply' }) {
  const { error: connectionError } = useSuiteConfig();
  const { tasks, loaded, readError, reading, refresh, submission, pending, lookupMissing, submit, dismissExpired } = useMaintenance();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [plan, setPlan] = useState<ApplyPlan | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [validating, setValidating] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const draftNames = useSyncExternalStore(subscribeDrafts, dirtyFileNames);
  const unsaved = !!draftNames;
  const restartResult = useRef<HTMLDivElement>(null);
  const applyResult = useRef<HTMLDivElement>(null);
  const cacheResult = useRef<HTMLDivElement>(null);
  const active = tasks.find(isActiveTask);
  const blocked = !loaded || !!readError || !!connectionError || !!active || !!pending || validating;
  const relevant = (action: MaintenanceTask['action']) => mode === 'cache' ? action === 'save_redis_snapshot'
    : mode === 'system' || (action === 'restart_dns_core' && configAction !== 'apply' && caps.deployment === 'native') || (action === 'apply_custom_mod' && configAction !== 'restart');
  const canRetry = Boolean(pending && lookupMissing && !requestExpired(pending) && !active && !connectionError && !readError
    && (pending.action === 'save_redis_snapshot' || !unsaved) && submission?.phase !== 'sending');

  useEffect(() => {
    const invalidate = () => { setPlan(null); setPreviewError(''); };
    window.addEventListener('lovestdns-config-changed', invalidate);
    return () => window.removeEventListener('lovestdns-config-changed', invalidate);
  }, []);

  const preview = async () => {
    if (dirtyFileDrafts() || blocked || !caps.features.config_apply) return;
    setValidating(true); setPreviewError(''); setPlan(null);
    try { setPlan(await suiteFetch<ApplyPlan>('/api/config/apply-plan')); }
    catch (error) { setPreviewError(errorText(error)); }
    finally { setValidating(false); }
  };

  const perform = async (action: MaintenanceTask['action'], result: HTMLDivElement | null, expected?: string) => {
    if (blocked || (action !== 'save_redis_snapshot' && dirtyFileDrafts())) return;
    result?.focus({ preventScroll: true });
    if (await submit(action, expected)) {
      setConfirmRestart(false);
      setPlan(null);
    }
  };

  const feedback = (action: MaintenanceTask['action']) => {
    if (submission?.action === action) {
      if (submission.phase === 'sending') return <strong>正在提交{TASK_NAMES[action]}请求…</strong>;
      if (submission.phase === 'failed') return <p>提交失败。{submission.detail}</p>;
      return <div className="suite-task-result">
        <strong>{pending?.retryRejected ? '重试被拒绝，正在核对原请求' : lookupMissing ? '暂未查到受理记录' : '暂无法确认是否已受理，正在核对原请求'}</strong>
        {pending && <small>请求 {pending.request_id.slice(0, 12)}</small>}
        {submission.detail && <details><summary>请求详情</summary>{submission.detail}</details>}
        {canRetry && <button className="btn btn-secondary btn-sm" onClick={() => void submit(action, pending?.expected_sha256)}>用原编号重试{TASK_NAMES[action]}</button>}
        {pending && lookupMissing && requestExpired(pending) && <button className="btn btn-secondary btn-sm" onClick={dismissExpired}>关闭已过期的请求</button>}
      </div>;
    }
    const recent = tasks.find(task => task.action === action);
    if (recent) return <TaskResult task={recent} historical={!!readError || !!connectionError} next={mode === 'config' ? '#config-query-domain' : undefined} />;
    return null;
  };

  const blockedReason = () => {
    if (connectionError || readError) return '连接暂不可用，恢复后可提交。';
    if (!loaded) return '正在读取任务状态…';
    if (active) return relevant(active.action) ? '' : `${TASK_NAMES[active.action]}正在处理，请等待任务结束。`;
    if (pending && !relevant(pending.action)) return '上一条请求仍待核对。';
    return '';
  };

  const highlighted = mode === 'system' ? tasks.filter(task => isActiveTask(task)
    || (['failed', 'interrupted'].includes(task.state) && tasks.find(latest => latest.action === task.action) === task)) : [];
  const history = mode === 'system' ? tasks.filter(task => !highlighted.includes(task)) : [];

  return <>
    {mode === 'config' && unsaved && <div className="suite-notice is-warning" role="status"><div>请先保存或还原以下草稿。<div className="suite-local-links">{draftNames.split(',').map(name => <a key={name} href={configFileLink(name)}>{configFileLabel(name)}</a>)}</div></div></div>}
    {blockedReason() && <p className="suite-note" role="status">{blockedReason()} <a href="#/system?tab=tasks">查看维护任务</a></p>}
    {!active && (loaded || readError) && caps.maintenance && !caps.maintenance.available && <div className="suite-notice is-warning" role="status"><div>{WORKER_PHASES[caps.maintenance.phase] ?? '暂时无法提交维护操作'}。<a href="#/system?component=manager">查看管理日志</a></div></div>}
    <div className={mode === 'config' ? 'suite-apply-options' : 'suite-maintenance-options'}>
      {mode === 'config' && configAction !== 'apply' && caps.deployment === 'native' && <div className="card">
        <div className="card-title"><RefreshCw size={17} />重启 DNS 以应用配置</div>
        <p className="suite-note">重新加载已保存的基础设置和 Unbound 配置。解析会短暂中断。</p>
        {confirmRestart && !pending && !active ? <div className="suite-apply-plan">
          <p>将保存 Redis 快照并重启 DNS，应用全部已保存的初始化配置。启动失败时，系统会尝试恢复此前的运行配置。</p>
          <div className="suite-local-links">
            <button className="btn btn-primary" disabled={blocked || unsaved || !caps.features.restart_dns_core} onClick={() => void perform('restart_dns_core', restartResult.current)}>确认重启 DNS</button>
            <button className="btn btn-secondary" onClick={() => setConfirmRestart(false)}>取消</button>
          </div>
        </div> : <button className="btn btn-secondary" disabled={blocked || unsaved || !caps.features.restart_dns_core} onClick={() => setConfirmRestart(true)}>查看重启范围</button>}
        {!active && !blocked && !caps.features.restart_dns_core && caps.maintenance?.available && <p className="suite-note">DNS 核心尚未就绪。<a href="#/system?component=bootstrap">查看启动日志</a></p>}
        <div className="suite-action-status" tabIndex={-1} ref={restartResult} role="status">{feedback('restart_dns_core')}</div>
      </div>}
      {mode === 'config' && configAction !== 'apply' && caps.deployment !== 'native' && <div className="card">
        <div className="card-title"><RefreshCw size={17} />应用基础与 Unbound 配置</div>
        <p className="suite-note">在宿主机更新配置并重新创建容器。解析可能短暂中断，随后可查询目标域名。</p>
        <a className="btn btn-secondary" href="#/deploy">查看主机操作说明</a>
      </div>}
      {mode === 'config' && configAction !== 'restart' && <div className="card">
        <div className="card-title"><FileCheck size={17} />应用自定义分流</div>
        <p className="suite-note">校验已保存的 custom_mod.yaml，再加载生成的 MosDNS 配置。</p>
        <button className="btn btn-secondary" disabled={blocked || unsaved || !caps.features.config_apply} onClick={() => void preview()}>
          {validating ? '正在校验…' : '预览并校验当前配置'}</button>
        {previewError && <p role="alert">校验未完成。{previewError}</p>}
        {!blocked && !caps.features.config_apply && <p className="suite-note">当前配置模式无法执行网页分流应用。<a href="#/system?component=mosdns">查看 MosDNS 状态</a></p>}
        {plan && !pending && !active && <div className="suite-apply-plan"><p>{plan.notice}</p>
          <details><summary>本次输入与生成的运行配置</summary><ul>{Object.entries(plan.inputs).map(([name, checksum]) => <li key={name}><code>{name}</code> · {checksum.slice(0, 12)}</li>)}</ul>
            <pre className="suite-log">{plan.candidate}</pre></details>
          <button className="btn btn-primary" disabled={blocked || unsaved || !caps.features.config_apply} onClick={() => void perform('apply_custom_mod', applyResult.current, plan.sha256)}>应用此预览配置</button>
        </div>}
        <div className="suite-action-status" tabIndex={-1} ref={applyResult} role="status">{feedback('apply_custom_mod')}</div>
      </div>}
      {mode === 'cache' && <div className="card"><div className="card-title"><Database size={17} />Redis 持久化维护</div>
        <dl className="suite-counters"><div><dt>默认 RDB 文件</dt><dd>{bytes(runtime?.persistence?.rdb_bytes)}</dd></div>
          <div><dt>数据盘可用空间</dt><dd>{bytes(runtime?.persistence?.disk_free_bytes)}</dd></div>
          <div><dt>最近后台保存</dt><dd>{runtime?.persistence?.last_save_status ?? '未采集'}</dd></div>
          <div><dt>自上次保存后的修改数</dt><dd>{runtime?.persistence?.changes_since_save ?? '未采集'}</dd></div></dl>
        {runtime?.persistence?.warnings.map(warning => <p role="status" key={warning}>{warning}</p>)}
        {runtime?.persistence?.storage_error && <p role="status">{runtime.persistence.storage_error}</p>}
        <button className="btn btn-secondary" disabled={blocked || !caps.features.redis_snapshot} onClick={() => void perform('save_redis_snapshot', cacheResult.current)}>保存 Redis 快照</button>
        {!blocked && !caps.features.redis_snapshot && <p className="suite-note">当前实例暂无法保存快照。<a href="#/system?component=redis">查看 Redis 日志</a></p>}
        <p className="suite-note">覆盖 Redis 持久化数据，保存期间会占用额外内存和磁盘。</p>
        <div className="suite-action-status" tabIndex={-1} ref={cacheResult} role="status">{feedback('save_redis_snapshot')}</div>
      </div>}
    </div>
    {mode === 'system' && <div className="card suite-maintenance-tasks">
      <div className="card-title"><Clock size={17} />维护任务
        <button className="btn btn-secondary btn-sm" disabled={reading} aria-busy={reading} onClick={() => void refresh()}><RefreshCw size={13} />刷新任务</button></div>
      {readError && <p role="status">暂时无法读取任务，正在重连。<a href="#/system?component=manager">查看管理日志</a></p>}
      {submission && <div className="suite-action-status" role="status">{feedback(submission.action)}</div>}
      {!tasks.length && !readError && <p className="suite-note">{loaded ? '暂无维护任务。' : '正在读取维护任务…'}</p>}
      {(historyOpen ? [...highlighted, ...history] : highlighted).map(task => <div className="suite-task" key={task.id}><TaskResult task={task} historical={!!readError} /></div>)}
      {history.length > 0 && <button className="btn btn-secondary btn-sm" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>{historyOpen ? '收起历史任务' : `查看历史任务（${history.length}）`}</button>}
    </div>}
  </>;
}
