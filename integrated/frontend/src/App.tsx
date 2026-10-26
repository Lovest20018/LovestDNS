import { useEffect, useState } from 'react';
import type { ComponentType } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  Activity, Boxes, Cable, ChevronRight, CircleGauge, FileCog,
  HeartPulse, LayoutDashboard, ListTree, Moon, Network, ScrollText, Server, Settings2,
  ShieldCheck, Sun, TerminalSquare, Wrench, X,
} from 'lucide-react';
import OverviewPage from './pages/SuiteDashboard';
import MonitoringPage from './pages/SuiteMonitoring';
import DiagnosticsPage from './pages/SuiteDiagnostics';
import ConfigPage from './pages/SuiteConfig';
import SystemPage from './pages/SuiteSystem';
import DeployPage from './pages/SuiteDeploy';
import EnvConfigPage from './pages/EnvConfigPage';
import DomainListPage from './pages/DomainListPage';
import TtlRulesPage from './pages/TtlRulesPage';
import AdvancedConfigPage from './pages/AdvancedConfigPage';
import SuiteProvider from './SuiteProvider';
import { useSuite } from './suiteContext';
import { Status, Permission, Offline } from './components/SuiteUI';
import './suite.css';

type Page = 'dashboard' | 'monitoring' | 'diagnostics' | 'config' | 'env' | 'lists' | 'ttl' | 'advanced' | 'system' | 'deploy';
const PATHS: Record<Page, string> = {
  dashboard: '/', monitoring: '/monitoring', diagnostics: '/diagnostics', config: '/config',
  env: '/env', lists: '/lists', ttl: '/ttl', advanced: '/advanced', system: '/system', deploy: '/deploy',
};
const COMPONENTS: Record<Page, ComponentType> = {
  dashboard: OverviewPage, monitoring: MonitoringPage, diagnostics: DiagnosticsPage, config: ConfigPage,
  env: EnvConfigPage, lists: DomainListPage, ttl: TtlRulesPage, advanced: AdvancedConfigPage,
  system: SystemPage, deploy: DeployPage,
};
const NAV: { title: string; items: { key: Page; label: string; icon: LucideIcon }[] }[] = [
  { title: '监控', items: [{ key: 'dashboard', label: '运行总览', icon: LayoutDashboard }, { key: 'monitoring', label: '实时指标', icon: CircleGauge }] },
  { title: '诊断', items: [{ key: 'diagnostics', label: 'DNS 诊断', icon: HeartPulse }] },
  { title: '配置', items: [{ key: 'config', label: '配置中心', icon: FileCog }, { key: 'env', label: '环境变量', icon: Settings2 }, { key: 'lists', label: '域名列表', icon: ListTree }, { key: 'ttl', label: 'TTL 规则', icon: Cable }, { key: 'advanced', label: '高级配置', icon: Wrench }] },
  { title: '系统', items: [{ key: 'system', label: '维护与日志', icon: ScrollText }, { key: 'deploy', label: '集成版部署', icon: TerminalSquare }] },
];

function pageFromHash(): Page {
  const path = window.location.hash.replace(/^#/, '') || '/';
  return (Object.entries(PATHS).find(([, value]) => value === path)?.[0] as Page | undefined) ?? 'dashboard';
}

function Shell() {
  const { caps, runtime, error, runtimeError, loading, refresh, connect } = useSuite();
  const [page, setPage] = useState<Page>(() => pageFromHash());
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try { return localStorage.getItem('paopaodns-theme') === 'dark' ? 'dark' : 'light'; } catch { return 'light'; }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const [token, setToken] = useState(() => { try { return localStorage.getItem('paopaodns-token') ?? ''; } catch { return ''; } });
  const [connectionOpen, setConnectionOpen] = useState(false);
  const Active = COMPONENTS[page];
  const active = NAV.flatMap((group) => group.items).find((item) => item.key === page);

  useEffect(() => {
    const sync = () => { setPage(pageFromHash()); setMobileOpen(false); };
    window.addEventListener('hashchange', sync);
    if (!window.location.hash) window.history.replaceState(null, '', '#/');
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; try { localStorage.setItem('paopaodns-theme', theme); } catch { /* ignore */ } }, [theme]);
  const health = runtime?.core_healthy ? 'good' : runtime ? 'warning' : caps ? 'neutral' : 'error';

  return <div className="suite-app">
    <div className={`suite-sidebar-backdrop ${mobileOpen ? 'is-open' : ''}`} onClick={() => setMobileOpen(false)} />
    <aside className={`suite-sidebar ${mobileOpen ? 'is-open' : ''}`}>
      <div className="suite-brand"><div className="suite-brand-mark">P</div><div><strong>PaoPaoDNS</strong><span>DNS 管理与诊断</span></div><button className="suite-mobile-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={18} /></button></div>
      <div className="suite-instance"><div className="suite-instance-icon"><Network size={17} /></div><div><small>当前实例</small><strong>{caps?.name ?? '集成版 DNS'}</strong></div><Status value={health} label={runtime?.core_healthy ? '运行中' : runtime ? '需检查' : loading ? '连接中' : caps ? '未采集' : '离线'} /></div>
      <nav className="suite-nav">{NAV.map((group) => <div className="suite-nav-group" key={group.title}><span className="suite-nav-title">{group.title}</span>{group.items.map((item) => { const Icon = item.icon; return <a key={item.key} href={`#${PATHS[item.key]}`} className={page === item.key ? 'is-active' : ''}><Icon size={17} /><span>{item.label}</span>{page === item.key && <ChevronRight size={14} />}</a>; })}</div>)}</nav>
      <div className="suite-sidebar-footer"><div><ShieldCheck size={15} /><span>API v1 · Token 鉴权</span></div><small>面板 {caps?.version ?? '0.1.0-dev'}</small></div>
    </aside>
    <main className="suite-main">
      <header className="suite-topbar"><button className="suite-mobile-menu" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Boxes size={19} /></button><div className="suite-breadcrumb"><span>集成版</span><ChevronRight size={14} /><strong>{active?.label ?? '运行总览'}</strong></div><div className="suite-top-actions"><div className="suite-connection-wrap"><button className="suite-connection suite-connection-button" onClick={() => setConnectionOpen((open) => !open)} aria-expanded={connectionOpen}><i className={`is-${health}`} /><span>{caps?.access_role === 'observer' ? '只读连接' : caps ? '已连接' : '未连接'}</span><ChevronRight size={13} className={connectionOpen ? 'rotate-90' : ''} /></button>{connectionOpen && <div className="suite-connection-popover"><strong>连接到集成版 API</strong><p>管理 Token 可读写配置；只读 Token 仅用于观测和诊断。</p><input type="password" value={token} onChange={(e) => setToken(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { connect(token); setConnectionOpen(false); } }} placeholder="输入 Bearer Token" autoFocus /><div><button className="btn btn-primary btn-sm" onClick={() => { connect(token); setConnectionOpen(false); }}>连接</button><button className="btn btn-secondary btn-sm" onClick={() => { connect(''); setConnectionOpen(false); }}>清除</button></div></div>}</div><button className="suite-icon-button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label="切换主题">{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button><button className="suite-refresh" onClick={() => void refresh()} disabled={loading}><Activity size={16} className={loading ? 'spin' : ''} />刷新</button></div></header>
      <section className="suite-content"><div className="suite-page-heading"><div><span className="suite-kicker">CONTROL CENTER / {active?.label?.toUpperCase() ?? 'OVERVIEW'}</span><h1>{active?.label ?? '运行总览'}</h1></div><div className="suite-page-meta"><span><Server size={15} /> {caps?.instance_id?.slice(0, 8) ?? '等待实例'}</span><span><Activity size={15} /> {runtime ? `采样于 ${new Date(runtime.sampled_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}` : '暂无运行采样'}</span></div></div>
        {error && <div className="suite-global-error"><HeartPulse size={16} /><div><strong>API 连接异常</strong><p>{error}</p></div><label>Token<input type="password" value={token} onChange={(e) => setToken(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') connect(token); }} placeholder="输入后回车重试" /></label></div>}
        {runtimeError && !error && <div className="suite-global-warning"><Activity size={16} /><span>运行采集暂不可用：{runtimeError}</span></div>}
        {['env', 'lists', 'ttl', 'advanced'].includes(page) ? caps ? <Permission allowed={caps.permissions.configure}><Active key={`${page}-${caps.access_role}`} /></Permission> : <Offline /> : <Active key={caps?.access_role ?? 'disconnected'} />}
      </section>
    </main>
  </div>;
}

export default function App() { return <SuiteProvider><Shell /></SuiteProvider>; }
