import { useEffect, useRef, useState } from 'react';
import { useSuite } from '../suiteContext';
import type { SaveResult } from '../api';
import SuiteMaintenance from './SuiteMaintenance';
import ConfigVerification from './ConfigVerification';
import { configFileLabel } from '../configFiles';
import { isActiveTask, useMaintenance } from '../maintenanceContext';
import '../pages/HomeWorkflow.css';

export function ConfigNextStep({ message, failed = false }: { message?: string; failed?: boolean }) {
  if (!message) return null;
  return <p className={`suite-save-feedback${failed ? ' is-error' : ''}`} role={failed ? 'alert' : 'status'}>
    <span>{message}</span>
    {!failed && <button className="suite-save-next" type="button" onClick={() => {
      const panel = document.getElementById('config-next-step') as HTMLDetailsElement | null;
      if (!panel) return;
      panel.open = true;
      panel.scrollIntoView({ block: 'start' });
      panel.querySelector('summary')?.focus({ preventScroll: true });
    }}>查看下一步</button>}
  </p>;
}

export default function ConfigFeedback({ saved, dirty, file, result, change }: { saved: boolean; dirty: boolean; file: string; result?: SaveResult; change?: { summary: string; domain?: string } }) {
  const { caps, runtime } = useSuite();
  const { tasks, pending } = useMaintenance();
  const panel = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(false);
  const restart = result?.requires_restart || ['custom_env.ini', 'unbound_custom.conf'].includes(file);
  const apply = result?.requires_reload || file === 'custom_mod.yaml';
  const action = restart ? 'restart_dns_core' : apply ? 'apply_custom_mod' : null;
  const recent = action ? tasks.find(task => task.action === action) : undefined;
  const processing = !!action && (pending?.action === action || (recent !== undefined && isActiveTask(recent)));
  useEffect(() => {
    // Once opened for work in progress, leave the result in view when it finishes.
    if (panel.current && (saved || processing)) panel.current.open = true;
  }, [saved, result, processing]);
  const next = restart ? caps?.deployment === 'native' ? '重启 DNS 加载' : '在宿主机加载'
    : apply ? '校验并应用分流' : result?.watched_now ? '验证域名' : '核对监听条件与查询';
  return <details id="config-next-step" className="suite-config-workflow" ref={panel} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{dirty ? '有未保存修改 · 应用与查询验证' : saved ? `已保存 · ${next}` : processing ? '查看正在处理的任务' : recent ? '应用与查询验证 · 查看最近任务' : '应用与查询验证'}</summary>
    {open && <><p className="suite-note"><strong>{configFileLabel(file)}</strong>{change && ` · ${change.summary}`}{!action && (result?.condition ? ` · ${result.condition}` : result?.watched_now ? ' · 文件处于监听范围，可查询域名确认结果。' : '')}</p>
      {caps?.permissions.maintain && (restart || apply) && <SuiteMaintenance caps={caps} runtime={runtime} mode="config" configAction={restart ? 'restart' : 'apply'} />}
      <ConfigVerification file={file} initialDomain={change?.domain} />
      <a href="#/system?tab=history">配置历史与恢复</a>
    </>}
  </details>;
}
