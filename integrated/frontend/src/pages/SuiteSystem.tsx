import { useCallback, useEffect, useRef, useState } from 'react';
import { ScrollText, Server } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { suiteFetch, errorText } from '../suiteApi';
import ConfigState from '../components/ConfigState';
import SuiteHistory from '../components/SuiteHistory';
import SuiteMaintenance from '../components/SuiteMaintenance';
import { Empty, Notice, Permission, Section } from '../components/SuiteUI';
import './SuitePage.css';

const COMPONENTS = ['bootstrap', 'mosdns', 'unbound-raw', 'unbound-forward', 'redis', 'dnscrypt', 'dnscrypt-socks', 'watcher', 'manager', 'observer', 'query-recorder', 'cron', 'updater'];
function componentFromHash() {
  const value = new URLSearchParams(window.location.hash.split('?')[1]).get('component');
  return value && COMPONENTS.includes(value) ? value : COMPONENTS[0];
}
export default function SuiteSystem() {
  const { caps, runtime } = useSuite();
  const [historyOpen, setHistoryOpen] = useState(false);
  const [component, setComponent] = useState(componentFromHash);
  const [logs, setLogs] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const changeComponent = useCallback((value: string) => {
    request.current?.abort(); setComponent(value); setLogs(''); setError('');
  }, []);
  useEffect(() => {
    const sync = () => {
      if (window.location.hash.split('?')[0] !== '#/system') return;
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
    if (!allowed) return;
    const timer = setTimeout(() => void loadLogs(), 0);
    return () => { clearTimeout(timer); request.current?.abort(); };
  }, [allowed, loadLogs]);
  useEffect(() => { if (new URLSearchParams(window.location.hash.split('?')[1]).get('tab') === 'apply') document.getElementById('config-apply')?.scrollIntoView({ block: 'start' }); }, [caps]);
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可查看维护任务和组件日志。</Empty>;
  return <div className="suite-page"><Permission allowed={caps.permissions.maintain} kind="maintain">
    <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/deploy">服务信息与主机命令</a><a className="btn btn-secondary btn-sm" href="#/status">服务状态</a></div>
    <div id="config-apply"><ConfigState /><SuiteMaintenance caps={caps} runtime={runtime} /></div><details className="suite-config-history" onToggle={(event) => setHistoryOpen(event.currentTarget.open)}><summary>配置历史与恢复</summary>{historyOpen && <SuiteHistory />}</details>
    <div id="component-logs"><Section title="组件日志" subtitle="最近 200 行，后端会脱敏 Token 和敏感环境变量" icon={ScrollText}>
      <div className="suite-query-form"><label>组件<select aria-label="日志组件" className="form-select" disabled={busy} value={component} onChange={(e) => { window.location.hash = `#/system?component=${encodeURIComponent(e.target.value)}`; }}>{COMPONENTS.map((name) => <option key={name}>{name}</option>)}</select></label><button className="btn btn-secondary" disabled={busy} onClick={() => void loadLogs()}><ScrollText size={16} />{busy ? '读取中…' : '读取日志'}</button></div>
      {error && <Notice tone="error">{error}</Notice>}{logs && <pre className="suite-log">{logs}</pre>}
    </Section></div>
  </Permission><Section title="当前实例信息" subtitle="查看本机版本、实例标识与核心来源" icon={Server}>
    <dl className="suite-kpi-list"><div><dt>实例 ID</dt><dd><code>{caps.instance_id}</code></dd></div><div><dt>版本</dt><dd>{caps.version}</dd></div><div><dt>核心来源</dt><dd><code>{caps.core.source_commit}</code></dd></div><div><dt>Redis</dt><dd>{runtime?.redis?.available ? '可采集' : '不可用'}</dd></div></dl>
  </Section></div>;
}
