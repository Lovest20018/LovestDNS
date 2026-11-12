import { useState } from 'react';
import { useSuite } from '../suiteContext';
import type { SaveResult } from '../api';
import SuiteMaintenance from './SuiteMaintenance';
import ConfigVerification from './ConfigVerification';
import '../pages/HomeWorkflow.css';

export default function ConfigFeedback({ saved, dirty, file, result }: { saved: boolean; dirty: boolean; file: string; result?: SaveResult }) {
  const { caps, runtime } = useSuite();
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const open = expanded ?? saved;
  const restart = result?.requires_restart || ['custom_env.ini', 'unbound_custom.conf'].includes(file);
  const apply = result?.requires_reload || file === 'custom_mod.yaml';
  return <details className="suite-config-workflow" open={open} onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary>{dirty ? '有未保存修改 · 应用与查询验证' : saved ? '已保存 · 继续应用与查询验证' : '应用与查询验证'}</summary>
    {open && <><p className="suite-note"><code>{file}</code> · {restart ? '需要重新生成运行配置。' : apply ? '预览并应用自定义规则。' : result?.condition ?? '根据核心启动配置判断自动加载条件；加载结果需查询核对。'}</p>
      {caps?.permissions.maintain && (restart || apply) && <SuiteMaintenance caps={caps} runtime={runtime} mode="config" configAction={restart ? 'restart' : 'apply'} />}
      <ConfigVerification file={file} />
      <a href="#/system?tab=apply">配置历史与维护日志</a>
    </>}
  </details>;
}
