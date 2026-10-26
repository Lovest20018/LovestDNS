import { useState } from 'react';
import { ScrollText, Server } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import SuiteMaintenance from '../components/SuiteMaintenance';
import { Empty, Notice, Permission, Section } from '../components/SuiteUI';
import './SuitePage.css';

const COMPONENTS = ['mosdns', 'unbound-raw', 'unbound-forward', 'redis', 'dnscrypt', 'dnscrypt-socks', 'watcher'];
export default function SuiteSystem() {
  const { caps, runtime } = useSuite();
  const [component, setComponent] = useState(COMPONENTS[0]);
  const [logs, setLogs] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可查看维护任务和组件日志。</Empty>;
  const loadLogs = async () => {
    setError(''); setLogs(''); setBusy(true);
    try { const result = await suiteFetch<{ lines: string[]; error?: string }>(`/api/v1/core-logs/${component}`); setLogs(result.error ?? (result.lines.join('\n') || '暂无日志')); }
    catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };
  return <div className="suite-page"><Permission allowed={caps.permissions.maintain} kind="maintain">
    <SuiteMaintenance caps={caps} runtime={runtime} />
    <Section title="组件日志" subtitle="最近 200 行，后端会脱敏 Token 和敏感环境变量" icon={ScrollText}>
      <div className="suite-query-form"><label>组件<select aria-label="日志组件" className="form-select" disabled={busy} value={component} onChange={(e) => { setComponent(e.target.value); setLogs(''); }}>{COMPONENTS.map((name) => <option key={name}>{name}</option>)}</select></label><button className="btn btn-secondary" disabled={busy} onClick={() => void loadLogs()}><ScrollText size={16} />{busy ? '读取中…' : '读取日志'}</button></div>
      {error && <Notice tone="error">{error}</Notice>}{logs && <pre className="suite-log">{logs}</pre>}
    </Section>
  </Permission><Section title="当前实例信息" subtitle="供独立家庭运维平台识别此 DNS 实例" icon={Server}>
    <dl className="suite-kpi-list"><div><dt>实例 ID</dt><dd><code>{caps.instance_id}</code></dd></div><div><dt>版本</dt><dd>{caps.version}</dd></div><div><dt>核心来源</dt><dd><code>{caps.core.source_commit}</code></dd></div><div><dt>Redis</dt><dd>{runtime?.redis?.available ? '可采集' : '不可用'}</dd></div></dl>
  </Section></div>;
}
