import { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle, Clock, Database, FileCheck, RefreshCw } from 'lucide-react';
import { suiteFetch, errorText } from '../suiteApi';
import { useSuite } from '../suiteContext';
import type { ApplyPlan, Capabilities, MaintenanceTask, RuntimeSnapshot } from '../suiteApi';

const STATES = { queued: '等待执行', running: '执行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' };

function bytes(value: number | undefined | null) {
  return typeof value === 'number' ? `${(value / 1024 / 1024).toFixed(1)} MiB` : '未采集';
}

export default function SuiteMaintenance({ caps, runtime, mode = 'system' }: { caps: Capabilities; runtime: RuntimeSnapshot | null; mode?: 'config' | 'system' }) {
  const { refresh: refreshRuntime, error: connectionError } = useSuite();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pollError, setPollError] = useState('');
  const [taskRefreshing, setTaskRefreshing] = useState(false);
  const knownTasks = useRef(new Map<string, string>());
  const [plan, setPlan] = useState<ApplyPlan | null>(null);
  const [tasks, setTasks] = useState<MaintenanceTask[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const pendingRequests = useRef<Record<string, string>>({});
  const enabled = caps.deployment === 'native' || caps.features.config_apply || caps.features.redis_snapshot;
  const active = tasks.some((task) => task.state === 'queued' || task.state === 'running');
  const recentResult = tasks.find((task) => !['queued', 'running'].includes(task.state));
  const visibleTasks = tasks.filter((task) => task === recentResult || ['queued', 'running'].includes(task.state));
  const hiddenCount = tasks.length - visibleTasks.length;

  const receiveTasks = useCallback((items: MaintenanceTask[]) => {
    const completed = items.some((task) => ['queued', 'running'].includes(knownTasks.current.get(task.id) ?? '') && ['succeeded', 'failed', 'interrupted'].includes(task.state));
    items.forEach((task) => knownTasks.current.set(task.id, task.state));
    setTasks(items); setPollError('');
    if (completed) { setPlan(null); void refreshRuntime(); }
  }, [refreshRuntime]);
  const refresh = useCallback(async () => {
    setTaskRefreshing(true);
    try { const result = await suiteFetch<{ tasks: MaintenanceTask[] }>('/api/maintenance/tasks'); receiveTasks(result.tasks); }
    catch (e) { setPollError(errorText(e)); }
    finally { setTaskRefreshing(false); }
  }, [receiveTasks]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await suiteFetch<{ tasks: MaintenanceTask[] }>('/api/maintenance/tasks');
        if (!cancelled) receiveTasks(result.tasks);
      } catch (e) { if (!cancelled) setPollError(errorText(e)); }
      if (!cancelled) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [enabled, receiveTasks]);

  const preview = async () => {
    setBusy(true); setError(''); setPlan(null);
    try { setPlan(await suiteFetch<ApplyPlan>('/api/config/apply-plan')); }
    catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };

  const submit = async (action: MaintenanceTask['action']) => {
    const expected = action === 'apply_custom_mod' ? plan?.sha256 : null;
    if (action === 'apply_custom_mod' && !expected) return;
    setBusy(true); setError('');
    const key = action + (expected ?? '');
    const id = pendingRequests.current[key] ?? crypto.randomUUID().replaceAll('-', '');
    pendingRequests.current[key] = id;
    try {
      const response = await suiteFetch<{ task: MaintenanceTask }>('/api/maintenance/tasks', { method: 'POST',
        body: JSON.stringify({ action, request_id: id, expected_sha256: expected }) });
      knownTasks.current.set(response.task.id, response.task.state);
      if (['succeeded', 'failed'].includes(response.task.state)) void refreshRuntime();
      setTasks((old) => [response.task, ...old.filter((task) => task.id !== response.task.id)].slice(0, 20));
      delete pendingRequests.current[key];
      setConfirmRestart(false);
      if (action === 'apply_custom_mod') setPlan(null);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };

  return <>
    <div className={mode === 'config' ? 'suite-apply-options' : 'suite-maintenance-options'}>
    {caps.deployment === 'native' && <div className="card"><div className="card-title"><RefreshCw size={17} />重启 DNS 核心</div>
      <p className="suite-note">保存基础设置、custom_env.ini 或 Unbound 配置后，在此重新生成运行配置。解析会短暂中断，页面和登录会话保持可用；任务完成后仍需查询目标域名验证。</p>
      {!confirmRestart ? <button className="btn btn-secondary" disabled={!caps.features.restart_dns_core || busy || active || !!connectionError} onClick={() => setConfirmRestart(true)}>重启 DNS 核心以应用配置</button> : <div className="suite-apply-plan" role="alert"><p>即将保存 Redis 快照并重启 DNS 组件。初始化失败会尝试恢复旧运行配置；编辑文件仍保留，便于修正。确认现在执行？</p><div className="suite-local-links"><button className="btn btn-primary" disabled={busy || active || !!connectionError} onClick={() => void submit('restart_dns_core')}>确认重启</button><button className="btn btn-secondary" disabled={busy} onClick={() => setConfirmRestart(false)}>取消</button></div></div>}
      {!caps.features.restart_dns_core && <p className="suite-note">维护服务尚未准备好，请稍后刷新或检查组件日志。</p>}
    </div>}
    {mode === 'config' && caps.deployment !== 'native' && <div className="card"><div className="card-title"><RefreshCw size={17} />应用基础与 Unbound 配置</div><p className="suite-note">初始化参数需要通过主机更新容器配置并重新创建容器。解析可能短暂中断，完成后请查询目标域名验证。</p><a className="btn btn-secondary" href="#/deploy">查看主机操作说明</a></div>}
    {mode === 'config' && <div className="card"><div className="card-title"><FileCheck size={17} />配置应用</div>
      <p className="suite-note">custom_mod.yaml 可单独预览并应用。{caps.deployment === 'native' ? '基础设置和 Unbound 配置请使用“重启 DNS 核心”。' : '基础设置和 Unbound 配置请按服务信息中的主机说明应用。'}初始化参数变更不适合仅重载 MosDNS。</p>
      <button className="btn btn-secondary" disabled={!caps.features.config_apply || busy || active || !!connectionError} onClick={() => void preview()}>
        预览并校验当前配置</button>
      {!caps.features.config_apply && <p className="suite-note">当前运行模式或维护服务尚不支持配置应用。</p>}
      {plan && <div className="suite-apply-plan"><p>{plan.notice}</p>
        <details><summary>本次输入与生成的运行配置</summary><ul>{Object.entries(plan.inputs).map(([name, checksum]) => <li key={name}><code>{name}</code> · {checksum.slice(0, 12)}</li>)}</ul>
          <pre className="suite-log">{plan.candidate}</pre></details>
        <button className="btn btn-primary" disabled={busy || active || !!connectionError} onClick={() => void submit('apply_custom_mod')}>应用此预览配置</button></div>}
    </div>}
    {mode === 'system' && <div className="card"><div className="card-title"><Database size={17} />Redis 持久化维护</div>
      <dl className="suite-counters"><div><dt>默认 RDB 文件</dt><dd>{bytes(runtime?.persistence?.rdb_bytes)}</dd></div>
        <div><dt>数据盘可用空间</dt><dd>{bytes(runtime?.persistence?.disk_free_bytes)}</dd></div>
        <div><dt>最近后台保存</dt><dd>{runtime?.persistence?.last_save_status ?? '未采集'}</dd></div>
        <div><dt>自上次保存后的修改数</dt><dd>{runtime?.persistence?.changes_since_save ?? '未采集'}</dd></div></dl>
      {runtime?.persistence?.warnings.map((warning) => <p role="status" key={warning}>{warning}</p>)}
      {runtime?.persistence?.storage_error && <p role="status">{runtime.persistence.storage_error}</p>}
      <button className="btn btn-secondary" disabled={!caps.features.redis_snapshot || busy || active || !!connectionError} onClick={() => void submit('save_redis_snapshot')}>保存 Redis 快照</button>
      <p className="suite-note">后台保存会占用临时内存和磁盘。任务确认保存完成并记录结果，覆盖 Redis 持久化数据；其他组件的内存缓存、查询历史和完整数据备份需另行处理。</p>
    </div>}
    </div>
    <div className="card suite-maintenance-tasks"><div className="card-title"><Clock size={17} />维护任务
      <button className="btn btn-secondary btn-sm" disabled={taskRefreshing} aria-busy={taskRefreshing} onClick={() => void refresh()}><RefreshCw size={13} />刷新</button></div>
      {pollError && <p role="alert">任务状态读取暂不可用：{pollError}。请刷新重试；下方结果可能已过期。</p>}
      {error && <p role="alert">操作未完成：{error} <a href="#/system">查看组件日志</a></p>}
      {!tasks.length && <p className="suite-note">暂无维护任务。保存配置后按上方入口应用，再查询验证。</p>}
      {hiddenCount > 0 && <button className="btn btn-secondary btn-sm" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>{historyOpen ? '收起历史任务' : `查看历史任务（${hiddenCount}）`}</button>}
      {(historyOpen ? tasks : visibleTasks).map((task) => <div className="suite-task" key={task.id}>
        <strong>{{ apply_custom_mod: '应用自定义配置', save_redis_snapshot: '保存 Redis 快照', restart_dns_core: '重启 DNS 核心' }[task.action]}</strong>
        <span>{task.state === 'succeeded' ? <CheckCircle size={13} /> : <Clock size={13} />} {STATES[task.state]}</span>
        <time>{new Date(task.updated * 1000).toLocaleString()}</time>
        {task.result?.message && <p>{task.result.message}</p>}
        {task.result?.error && <p role="status">{task.result.error}</p>}
        {task.result?.rollback && <p>运行配置回退：{task.result.rollback.runtime_restored ? '已恢复' : '未确认'}；服务：{task.result.rollback.service_running ? '已启动' : '需检查'}。{task.result.rollback.error}</p>}
        {task.result?.saved_at && <p>保存时间：{new Date(task.result.saved_at * 1000).toLocaleString()} · {bytes(task.result.bytes)}</p>}
        {task.state === 'succeeded' && task.action !== 'save_redis_snapshot' && <p className="suite-task-next">配置任务已完成；规则效果尚未验证。<a href="#/diagnostics">查询目标域名验证</a></p>}
        {['failed', 'interrupted'].includes(task.state) && <p className="suite-task-next"><a href="#/system">查看日志</a> · <a href="#/">返回配置检查后重试</a></p>}
        <small className="suite-note">任务 {task.id.slice(0, 12)}</small>
      </div>)}
    </div>
  </>;
}
