import { useCallback, useEffect, useRef, useState } from 'react';
import { CheckCircle, Clock, Database, FileCheck, RefreshCw } from 'lucide-react';
import { suiteFetch, errorText } from '../suiteApi';
import type { ApplyPlan, Capabilities, MaintenanceTask, RuntimeSnapshot } from '../suiteApi';

const STATES = { queued: '等待执行', running: '执行中', succeeded: '已完成', failed: '失败', interrupted: '已中断' };

function bytes(value: number | undefined | null) {
  return typeof value === 'number' ? `${(value / 1024 / 1024).toFixed(1)} MiB` : '未采集';
}

export default function SuiteMaintenance({ caps, runtime, mode = 'system' }: { caps: Capabilities; runtime: RuntimeSnapshot | null; mode?: 'config' | 'system' }) {
  const [plan, setPlan] = useState<ApplyPlan | null>(null);
  const [tasks, setTasks] = useState<MaintenanceTask[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pendingRequests = useRef<Record<string, string>>({});
  const enabled = caps.features.config_apply || caps.features.redis_snapshot;
  const active = tasks.some((task) => task.state === 'queued' || task.state === 'running');

  const refresh = useCallback(async () => {
    const result = await suiteFetch<{ tasks: MaintenanceTask[] }>('/api/maintenance/tasks');
    setTasks(result.tasks);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await suiteFetch<{ tasks: MaintenanceTask[] }>('/api/maintenance/tasks');
        if (!cancelled) setTasks(result.tasks);
      } catch (e) { if (!cancelled) setError(errorText(e)); }
      if (!cancelled) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [enabled]);

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
      setTasks((old) => [response.task, ...old.filter((task) => task.id !== response.task.id)].slice(0, 20));
      delete pendingRequests.current[key];
      if (action === 'apply_custom_mod') setPlan(null);
    } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };

  return <>
    {mode === 'config' && <div className="card"><div className="card-title"><FileCheck size={17} />配置应用</div>
      <p className="suite-note">先在高级配置保存 custom_mod.yaml，再预览当前配置和引用文件。应用会重启 MosDNS，可能短暂影响解析；初始化变量和 Unbound 配置仍需重启容器。</p>
      <button className="btn btn-secondary" disabled={!caps.features.config_apply || busy || active} onClick={() => void preview()}>
        预览并校验当前配置</button>
      {!caps.features.config_apply && <p className="suite-note">当前运行模式或维护服务尚不支持配置应用。</p>}
      {plan && <div className="suite-apply-plan"><p>{plan.notice}</p>
        <details><summary>本次输入与生成的运行配置</summary><ul>{Object.entries(plan.inputs).map(([name, checksum]) => <li key={name}><code>{name}</code> · {checksum.slice(0, 12)}</li>)}</ul>
          <pre className="suite-log">{plan.candidate}</pre></details>
        <button className="btn btn-primary" disabled={busy || active} onClick={() => void submit('apply_custom_mod')}>应用此预览配置</button></div>}
    </div>}
    {mode === 'system' && <div className="card"><div className="card-title"><Database size={17} />Redis 持久化维护</div>
      <dl className="suite-counters"><div><dt>默认 RDB 文件</dt><dd>{bytes(runtime?.persistence?.rdb_bytes)}</dd></div>
        <div><dt>数据盘可用空间</dt><dd>{bytes(runtime?.persistence?.disk_free_bytes)}</dd></div>
        <div><dt>最近后台保存</dt><dd>{runtime?.persistence?.last_save_status ?? '未采集'}</dd></div>
        <div><dt>自上次保存后的修改数</dt><dd>{runtime?.persistence?.changes_since_save ?? '未采集'}</dd></div></dl>
      {runtime?.persistence?.warnings.map((warning) => <p role="status" key={warning}>{warning}</p>)}
      {runtime?.persistence?.storage_error && <p role="status">{runtime.persistence.storage_error}</p>}
      <button className="btn btn-secondary" disabled={!caps.features.redis_snapshot || busy || active} onClick={() => void submit('save_redis_snapshot')}>保存 Redis 快照</button>
      <p className="suite-note">后台保存会占用临时内存和磁盘。任务确认保存完成并记录结果，覆盖 Redis 持久化数据；其他组件的内存缓存、查询历史和完整数据备份需另行处理。</p>
    </div>}
    <div className="card"><div className="card-title"><Clock size={17} />维护任务
      <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { void refresh().catch((e) => setError(errorText(e))); }}><RefreshCw size={13} />刷新</button></div>
      {error && <p role="alert">{error}</p>}
      {!tasks.length && <p className="suite-note">暂无任务。维护服务重启后未完成的任务会标记为中断，请检查状态后再决定是否重试。</p>}
      {tasks.map((task) => <div className="suite-task" key={task.id}>
        <strong>{task.action === 'apply_custom_mod' ? '应用自定义配置' : '保存 Redis 快照'}</strong>
        <span>{task.state === 'succeeded' ? <CheckCircle size={13} /> : <Clock size={13} />} {STATES[task.state]}</span>
        <time>{new Date(task.updated * 1000).toLocaleString()}</time>
        {task.result?.message && <p>{task.result.message}</p>}
        {task.result?.error && <p role="status">{task.result.error}</p>}
        {task.result?.rollback && <p>运行配置回退：{task.result.rollback.runtime_restored ? '已恢复' : '未确认'}；服务：{task.result.rollback.service_running ? '已启动' : '需检查'}。{task.result.rollback.error}</p>}
        {task.result?.saved_at && <p>保存时间：{new Date(task.result.saved_at * 1000).toLocaleString()} · {bytes(task.result.bytes)}</p>}
        <small className="suite-note">任务 {task.id.slice(0, 12)}</small>
      </div>)}
    </div>
  </>;
}
