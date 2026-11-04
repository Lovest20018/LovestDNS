import { useCallback, useEffect, useRef, useState } from 'react';
import type { ComponentType } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  Activity, Boxes, Cable, ChevronRight, FileCog, HeartPulse, KeyRound,
  ListTree, LogOut, Moon, Network, ScrollText, Server, Settings2, Sun, Wrench, X,
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
import BrowserLogin from './components/BrowserLogin';
import SuiteProvider from './SuiteProvider';
import { useSuite } from './suiteContext';
import { browserAuth, errorText, setBrowserCsrf } from './suiteApi';
import type { BrowserSession } from './suiteApi';
import { Status, Permission, Offline } from './components/SuiteUI';
import { confirmDiscard } from './unsaved';
import './suite.css';

type Page = 'dashboard' | 'monitoring' | 'diagnostics' | 'config' | 'env' | 'lists' | 'ttl' | 'advanced' | 'system' | 'deploy';
const PATHS: Record<Page, string> = {
  config: '/', dashboard: '/status', monitoring: '/monitoring', diagnostics: '/diagnostics',
  env: '/env', lists: '/lists', ttl: '/ttl', advanced: '/advanced', system: '/system', deploy: '/deploy',
};
const TITLES: Record<Page, string> = {
  config: '配置中心', dashboard: '服务状态', monitoring: '运行指标', diagnostics: '查询与诊断',
  env: '基础设置', lists: '域名分流', ttl: 'TTL 规则', advanced: '高级配置', system: '维护与日志', deploy: '服务信息',
};
const COMPONENTS: Record<Page, ComponentType> = {
  dashboard: OverviewPage, monitoring: MonitoringPage, diagnostics: DiagnosticsPage, config: ConfigPage,
  env: EnvConfigPage, lists: DomainListPage, ttl: TtlRulesPage, advanced: AdvancedConfigPage,
  system: SystemPage, deploy: DeployPage,
};
const NAV: { title: string; items: { key: Page; icon: LucideIcon }[] }[] = [
  { title: 'DNS 配置', items: [{ key: 'config', icon: FileCog }, { key: 'env', icon: Settings2 }, { key: 'lists', icon: ListTree }, { key: 'ttl', icon: Cable }, { key: 'advanced', icon: Wrench }] },
  { title: '本地工具', items: [{ key: 'diagnostics', icon: HeartPulse }, { key: 'system', icon: ScrollText }] },
  { title: '实例信息', items: [{ key: 'dashboard', icon: Activity }, { key: 'deploy', icon: Server }] },
];
function pageFromHash(): Page {
  const path = window.location.hash.replace(/^#/, '') || '/';
  if (path === '/config') return 'config';
  return (Object.entries(PATHS).find(([, value]) => value === path)?.[0] as Page | undefined) ?? 'config';
}
type Theme = 'light' | 'dark';

function Shell({ theme, setTheme, onLogout, onPassword, actionError }: { theme: Theme; setTheme: (value: Theme) => void; onLogout: () => void; onPassword: () => void; actionError: string }) {
  const { caps, runtime, error, runtimeError, loading, refreshing, refresh } = useSuite();
  const [page, setPage] = useState<Page>(() => pageFromHash());
  const [mobileOpen, setMobileOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width:900px)').matches);
  useEffect(() => { const media = window.matchMedia('(max-width:900px)'); const update = () => { setNarrow(media.matches); if (!media.matches) setMobileOpen(false); }; media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  const Active = COMPONENTS[page];
  const sidebar = useRef<HTMLElement>(null);
  const menuButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!mobileOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    const opener = menuButton.current;
    sidebar.current?.querySelector<HTMLElement>('a, button')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setMobileOpen(false); }
      if (event.key !== 'Tab') return;
      const items = Array.from(sidebar.current?.querySelectorAll<HTMLElement>('a, button') ?? []).filter((item) => item.offsetParent !== null);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); (opener ?? previous)?.focus(); };
  }, [mobileOpen]);
  useEffect(() => {
    let previousHash = window.location.hash || '#/';
    const sync = () => {
      if (window.location.hash !== previousHash && !confirmDiscard()) { window.history.replaceState(null, '', previousHash); return; }
      previousHash = window.location.hash;
      setPage(pageFromHash()); setMobileOpen(false); window.scrollTo(0, 0);
    };
    window.addEventListener('hashchange', sync);
    if (!window.location.hash) window.history.replaceState(null, '', '#/');
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  const health = error ? 'error' : runtime?.core_healthy ? 'good' : runtime ? 'warning' : caps ? 'neutral' : 'error';
  return <div className="suite-app">
    <div className={`suite-sidebar-backdrop ${mobileOpen ? 'is-open' : ''}`} onClick={() => setMobileOpen(false)} />
    <aside inert={narrow && !mobileOpen} ref={sidebar} id="suite-navigation" aria-label="主导航" className={`suite-sidebar ${mobileOpen ? 'is-open' : ''}`}>
      <a href="#/" className="suite-brand"><img className="suite-brand-mark" src="/lovestdns-icon.svg" alt="" /><div><strong>LovestDNS</strong><span>DNS 管理</span></div></a>
      <button className="suite-mobile-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={18} /></button>
      <a href="#/monitoring" className="suite-instance"><div className="suite-instance-icon"><Network size={17} /></div><div><small>当前 DNS</small><strong>{caps?.name ?? 'LovestDNS'}</strong></div><Status value={health} label={error ? '连接中断' : runtime?.core_healthy ? '正常' : runtime ? '需检查' : loading ? '连接中' : caps ? '未采集' : '离线'} /></a>
      <nav className="suite-nav">{NAV.map((group) => <div className="suite-nav-group" key={group.title}><span className="suite-nav-title">{group.title}</span>{group.items.map((item) => { const Icon = item.icon; return <a key={item.key} href={`#${PATHS[item.key]}`} aria-current={page === item.key ? 'page' : undefined} className={page === item.key ? 'is-active' : ''}><Icon size={17} /><span>{TITLES[item.key]}</span>{page === item.key && <ChevronRight size={14} />}</a>; })}</div>)}</nav>
      <div className="suite-sidebar-footer"><span>本地配置与诊断</span><small>{caps?.deployment === 'native' ? '原生服务' : caps ? '容器服务' : 'LovestDNS'} · {caps?.version?.replace('0.1.0-dev.lovestdns-native.', '') ?? '准备中'}</small></div>
    </aside>
    <main className="suite-main" inert={mobileOpen}>
      <header className="suite-topbar"><button ref={menuButton} aria-controls="suite-navigation" aria-expanded={mobileOpen} className="suite-mobile-menu" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Boxes size={19} /></button><div className="suite-breadcrumb"><span>DNS</span><ChevronRight size={14} /><strong>{TITLES[page]}</strong></div><div className="suite-top-actions"><button className="suite-icon-button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} aria-label="切换主题">{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button><button className="suite-refresh" onClick={() => void refresh()} disabled={refreshing} aria-busy={refreshing} aria-label="刷新状态"><Activity size={16} className={refreshing ? 'spin' : ''} /><span>刷新</span></button><button className="suite-icon-button" onClick={onPassword} aria-label="修改管理员密码" title="修改密码"><KeyRound size={17} /></button><button className="suite-icon-button" onClick={onLogout} aria-label="退出登录" title="退出登录"><LogOut size={17} /></button></div></header>
      <section className="suite-content"><div className="suite-page-heading"><div><span className="suite-kicker">LOVESTDNS</span><h1>{TITLES[page]}</h1></div><div className="suite-page-meta"><span><Server size={15} /> {caps?.deployment === 'native' ? '原生服务' : caps?.deployment === 'container' ? '容器服务' : '等待实例'}</span><Status value={health} label={error ? '连接中断 · 状态待确认' : runtime?.core_healthy ? 'DNS 运行正常' : runtime ? 'DNS 状态需检查' : '等待状态采样'} /></div></div>
        {(error || actionError) && <div className="suite-global-error"><HeartPulse size={16} /><div><strong>{error ? '连接暂时中断，草稿已保留' : '操作未完成'}</strong><p>{error || actionError}</p></div><button className="btn btn-secondary btn-sm" onClick={() => void refresh()}>重试</button></div>}
        {runtimeError && !error && <div className="suite-global-warning"><Activity size={16} /><span>运行采集暂不可用：{runtimeError}</span></div>}
        {['env', 'lists', 'ttl', 'advanced'].includes(page) ? caps ? <Permission allowed={caps.permissions.configure}><Active key={`${page}-${caps.access_role}`} /></Permission> : <Offline /> : <Active key={caps?.access_role ?? 'disconnected'} />}
      </section>
    </main>
  </div>;
}

export default function App() {
  const [session, setSession] = useState<BrowserSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState('');
  const [actionError, setActionError] = useState('');
  const [changing, setChanging] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => {
    try { return localStorage.getItem('paopaodns-theme') === 'dark' ? 'dark' : 'light'; } catch { return 'light'; }
  });
  const checkSession = useCallback(async () => {
    try { const value = await browserAuth('session'); setSession(value); setAuthError(''); }
    catch (e) { setSession(null); setAuthError(errorText(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    // Browser credentials now live only in an HttpOnly cookie.
    try { localStorage.removeItem('paopaodns-token'); } catch { /* Storage may be unavailable. */ }
    let cancelled = false;
    void browserAuth('session').then((value) => { if (!cancelled) { setSession(value); setAuthError(''); } })
      .catch((e) => { if (!cancelled) { setSession(null); setAuthError(errorText(e)); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    const expired = () => { setBrowserCsrf(''); setChanging(false); setLoading(true); void checkSession(); };
    window.addEventListener('lovestdns-auth-expired', expired);
    return () => { cancelled = true; window.removeEventListener('lovestdns-auth-expired', expired); };
  }, [checkSession]);
  useEffect(() => { document.documentElement.dataset.theme = theme; try { localStorage.setItem('paopaodns-theme', theme); } catch { /* ignore */ } }, [theme]);
  const logout = async () => {
    if (!confirmDiscard()) return;
    setActionError('');
    try { setSession(await browserAuth('logout', {})); setChanging(false); }
    catch (e) { setActionError(errorText(e)); }
  };
  if (!session?.authenticated || session.must_change_password || changing) return <BrowserLogin key={changing || session?.must_change_password ? 'password' : 'login'} session={session} loading={loading} error={authError} changePassword={changing} onAuthenticated={(value) => { setSession(value); setChanging(false); setActionError(''); }} onCancel={() => setChanging(false)} onRetry={() => { setLoading(true); void checkSession(); }} />;
  return <SuiteProvider><Shell theme={theme} setTheme={setTheme} onLogout={() => void logout()} onPassword={() => { if (confirmDiscard()) setChanging(true); }} actionError={actionError} /></SuiteProvider>;
}
