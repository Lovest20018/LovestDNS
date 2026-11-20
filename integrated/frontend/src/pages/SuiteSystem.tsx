import { useCallback, useEffect, useRef, useState } from 'react';
import { ScrollText } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import RuntimeStatus from '../components/RuntimeStatus';
import SuiteHistory from '../components/SuiteHistory';
import SuiteMaintenance from '../components/SuiteMaintenance';
import SoftwareUpdate from '../components/SoftwareUpdate';
import { Empty, Notice, Permission, Section } from '../components/SuiteUI';
import './SuitePage.css';

const COMPONENTS = ['bootstrap', 'mosdns', 'unbound-raw', 'unbound-forward', 'redis', 'dnscrypt', 'dnscrypt-socks', 'watcher', 'manager', 'observer', 'query-recorder', 'cron', 'updater'];
function componentFromHash() {
  const value = new URLSearchParams(window.location.hash.split('?')[1]).get('component');
  return value && COMPONENTS.includes(value) ? value : COMPONENTS[0];
}
function tabFromHash() {
  const params = new URLSearchParams(window.location.hash.split('?')[1]);
  if (['#/status', '#/monitoring'].includes(window.location.hash.split('?')[0]) || params.get('tab') === 'status') return 'status';
  if (params.has('component') || params.get('tab') === 'logs') return 'logs';
  if (params.get('tab') === 'history') return 'history';
  return params.get('tab') === 'updates' ? 'updates' : 'tasks';
}
export default function SuiteSystem() {
  const { caps, runtime } = useSuite();
  const [tab, setTab] = useState(tabFromHash);
  const [component, setComponent] = useState(componentFromHash);
  const [logs, setLogs] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const changeComponent = useCallback((value: string) => {
    request.current?.abort(); setComponent(value); setLogs(''); setError('');
  }, []);
  useEffect(() => {
    const sync = () => {
      if (!['#/system', '#/status', '#/monitoring'].includes(window.location.hash.split('?')[0])) return;
      setTab(tabFromHash());
      const value = componentFromHash();
      if (value !== component) changeComponent(value);
    };
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, [component, changeComponent]);
  const loadLogs = useCallback(async () => {
    request.current?.abort();
    const abort = new AbortController(); request.current = abort;
    const signal = abort.signal;
    setError(''); setBusy(true);
    try {
      const result = await suiteFetch<{ lines: string[]; error?: string }>(`/api/v1/core-logs/${component}`, { signal });
      if (!signal.aborted) {
        setLogs(result.error ?? (result.lines.join('\n') || '暂无日志'));
        if (new URLSearchParams(window.location.hash.split('?')[1]).has('component')) requestAnimationFrame(() => document.getElementById('component-logs')?.scrollIntoView({ block: 'start' }));
      }
    }
    catch (e) { if (!signal.aborted) setError(errorText(e)); }
    finally { if (!signal.aborted) setBusy(false); }
  }, [component]);
  const allowed = caps?.permissions.maintain;
  useEffect(() => {
    if (!allowed || tab !== 'logs') return;
    const timer = setTimeout(() => void loadLogs(), 0);
    return () => { clearTimeout(timer); request.current?.abort(); };
  }, [allowed, loadLogs, tab]);
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可查看维护任务和组件日志。</Empty>;
  return <div className="suite-page">
    <nav className="suite-page-tabs" aria-label="维护分区">
      <a href="#/system?tab=status" aria-current={tab === 'status' ? 'page' : undefined}>运行状态</a>
      {caps.permissions.maintain && <>{[['tasks', '任务'], ['logs', '组件日志'], ['history', '配置历史'], ...(caps.deployment === 'native' ? [['updates', '软件更新']] : [])].map(([key, label]) =>
        <a key={key} aria-current={tab === key ? 'page' : undefined} href={`#/system?tab=${key}`}>{label}</a>)}</>}
      <a href="#/cache">缓存与快照</a>
    </nav>
    {tab === 'status' ? <RuntimeStatus /> : <Permission allowed={caps.permissions.maintain} kind="maintain">
    {tab === 'tasks' && <SuiteMaintenance caps={caps} runtime={runtime} />}
    {tab === 'history' && <Section title="配置历史与恢复" actions={<a href="#/config?tab=apply">前往应用配置</a>}><SuiteHistory /></Section>}
    {tab === 'updates' && (caps.deployment === 'native' ? <SoftwareUpdate loadedVersion={caps.version} /> : <Empty title="通过主机更新容器">请参阅服务信息中的部署说明。<a href="#/deploy">查看说明</a></Empty>)}
    {tab === 'logs' && <div id="component-logs"><Section title="组件日志" subtitle="最近 200 行，后端会脱敏 Token 和敏感环境变量" icon={ScrollText}>
      <div className="suite-query-form"><label>组件<select aria-label="日志组件" className="form-select" disabled={busy} value={component} onChange={(e) => { window.location.hash = `#/system?component=${encodeURIComponent(e.target.value)}`; }}>{COMPONENTS.map((name) => <option key={name}>{name}</option>)}</select></label><button className="btn btn-secondary" disabled={busy} onClick={() => void loadLogs()}><ScrollText size={16} />{busy ? '读取中…' : '读取日志'}</button></div>
      {error && <Notice tone="error">{error}</Notice>}{logs && <pre className="suite-log">{logs}</pre>}
    </Section></div>}
  </Permission>}<div className="suite-local-links"><a href="#/deploy">服务信息与主机命令</a><a href="#/config?tab=apply">应用解析配置</a></div></div>;
}
