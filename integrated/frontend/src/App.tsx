import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  Activity, Boxes, Cable, ChevronRight, FileCog, HeartPulse, KeyRound,
  LogOut, Moon, Network, ScrollText, Server, Sun, X,
} from 'lucide-react';
import BrowserLogin from './components/BrowserLogin';
import SuiteProvider from './SuiteProvider';
import GatewayProvider from './GatewayProvider';
import { useGateway } from './gatewayContext';
import { useSuite } from './suiteContext';
import { browserAuth, errorText, setBrowserCsrf } from './suiteApi';
import type { BrowserSession } from './suiteApi';
import { Status, Permission, Offline } from './components/SuiteUI';
import { confirmDiscard, useUnsavedChanges } from './unsaved';
import { clearFileDrafts } from './fileDrafts';
import { useStore } from './store';
import { clearMaintenanceRequests } from './maintenanceRequests';
import ConfigState from './components/ConfigState';
import PageBoundary from './components/PageBoundary';
import './suite.css';

type Page = 'cache' | 'dashboard' | 'diagnostics' | 'gateway' | 'config' | 'env' | 'lists' | 'ttl' | 'advanced' | 'system' | 'deploy';
const PATHS: Record<Page, string> = {
  cache: '/cache', config: '/config', dashboard: '/', diagnostics: '/diagnostics', gateway: '/gateway',
  env: '/env', lists: '/lists', ttl: '/ttl', advanced: '/advanced', system: '/system', deploy: '/deploy',
};
const TITLES: Record<Page, string> = {
  cache: '缓存', config: '解析配置', dashboard: '运行概览', diagnostics: '查询与诊断', gateway: '网关观测',
  env: '基础设置', lists: '域名分流', ttl: 'TTL 规则', advanced: '高级配置', system: '维护与日志', deploy: '设置',
};
const LOADERS = {
  cache: () => import('./pages/SuiteCache'), dashboard: () => import('./pages/SuiteDashboard'), diagnostics: () => import('./pages/SuiteDiagnostics'),
  gateway: () => import('./pages/SuiteGateway'),
  config: () => import('./pages/SuiteConfig'), system: () => import('./pages/SuiteSystem'), deploy: () => import('./pages/SuiteDeploy'),
  env: () => import('./pages/EnvConfigPage'), lists: () => import('./pages/DomainListPage'), ttl: () => import('./pages/TtlRulesPage'), advanced: () => import('./pages/AdvancedConfigPage'),
};
const COMPONENTS = {
  cache: lazy(LOADERS.cache), dashboard: lazy(LOADERS.dashboard), diagnostics: lazy(LOADERS.diagnostics), config: lazy(LOADERS.config),
  gateway: lazy(LOADERS.gateway),
  system: lazy(LOADERS.system), deploy: lazy(LOADERS.deploy), env: lazy(LOADERS.env),
  lists: lazy(LOADERS.lists), ttl: lazy(LOADERS.ttl), advanced: lazy(LOADERS.advanced),
};
function retryPage(page: Page) { COMPONENTS[page] = lazy(LOADERS[page]); }
const NAV: { title: string; items: { key: Page; icon: LucideIcon }[] }[] = [
  { title: 'DNS 控制台', items: [{ key: 'dashboard', icon: Activity }, { key: 'config', icon: FileCog }, { key: 'cache', icon: Server }, { key: 'diagnostics', icon: HeartPulse }, { key: 'gateway', icon: Cable }, { key: 'system', icon: ScrollText }, { key: 'deploy', icon: Network }] },
];
function navPage(page: Page): Page { return page === 'ttl' ? 'cache' : ['env', 'lists', 'advanced'].includes(page) ? 'config' : page; }
function pageFromHash(): Page {
  const path = window.location.hash.replace(/^#/, '').split('?')[0] || '/';
  if (path === '/settings') return 'deploy';
  if (path === '/diagnostics' && new URLSearchParams(window.location.hash.split('?')[1]).get('tab') === 'gateway') return 'gateway';
  if (path === '/config') return 'config';
  if (path === '/monitoring' || path === '/status') return 'dashboard';
  return (Object.entries(PATHS).find(([, value]) => value === path)?.[0] as Page | undefined) ?? 'dashboard';
}
type Theme = 'light' | 'dark' | 'system';

function Shell({ theme, setTheme, onLogout, onPassword, actionError }: { theme: Theme; setTheme: (value: Theme) => void; onLogout: () => void; onPassword: () => void; actionError: string }) {
  const { caps, runtime, error, runtimeError, runtimeStale, paused, loading, refreshing, refresh } = useSuite();
  const { config: gateway } = useGateway();
  const [page, setPage] = useState<Page>(() => pageFromHash());
  const [mobileOpen, setMobileOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width:900px)').matches);
  useEffect(() => { const media = window.matchMedia('(max-width:900px)'); const update = () => { setNarrow(media.matches); if (!media.matches) setMobileOpen(false); }; media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  const [pageAttempt, setPageAttempt] = useState(0);
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
    const sync = () => {
      setPage(pageFromHash()); setMobileOpen(false); window.scrollTo(0, 0);
    };
    window.addEventListener('hashchange', sync);
    if (!window.location.hash) window.history.replaceState(null, '', '#/');
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  const health = error ? 'error' : paused || runtimeStale ? 'neutral' : runtime?.core_healthy ? 'good' : runtime ? 'warning' : caps ? 'neutral' : 'error';
  return <div className="suite-app">
    <div className={`suite-sidebar-backdrop ${mobileOpen ? 'is-open' : ''}`} onClick={() => setMobileOpen(false)} />
    <aside inert={narrow && !mobileOpen} ref={sidebar} id="suite-navigation" aria-label="主导航" className={`suite-sidebar ${mobileOpen ? 'is-open' : ''}`}>
      <a href="#/" className="suite-brand"><img className="suite-brand-mark" src="/lovestdns-icon.svg" alt="" /><div><strong>LovestDNS</strong><span>DNS 管理面板</span></div></a>
      <button className="suite-mobile-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={18} /></button>
      <a href="#/status" className="suite-instance"><div className="suite-instance-icon"><Network size={17} /></div><div><small>当前 DNS</small><strong title={caps?.name ?? 'LovestDNS'}>{caps?.name ?? 'LovestDNS'}</strong></div><Status value={health} label={error ? '连接中断' : paused ? '采样已暂停' : runtimeStale ? '数据过期' : runtime?.core_healthy ? '正常' : runtime ? '需检查' : loading ? '连接中' : caps ? '未采集' : '离线'} /></a>
      <nav className="suite-nav">{NAV.map((group) => <div className="suite-nav-group" key={group.title}><span className="suite-nav-title">{group.title}</span>{group.items.filter((item) => item.key !== 'gateway' || gateway?.enabled).map((item) => { const Icon = item.icon; return <a key={item.key} href={`#${PATHS[item.key]}`} aria-current={navPage(page) === item.key ? 'page' : undefined} className={navPage(page) === item.key ? 'is-active' : ''}><Icon size={17} /><span>{TITLES[item.key]}</span>{navPage(page) === item.key && <ChevronRight size={14} />}</a>; })}</div>)}</nav>
      <div className="suite-sidebar-footer"><span>本地配置与诊断</span><small>{caps?.deployment === 'native' ? '原生服务' : caps ? '容器服务' : 'LovestDNS'} · {caps?.version?.replace('0.1.0-dev.lovestdns-native.', '') ?? '准备中'}</small></div>
    </aside>
    <main className="suite-main" inert={mobileOpen}>
      <header className="suite-topbar"><button ref={menuButton} aria-controls="suite-navigation" aria-expanded={mobileOpen} className="suite-mobile-menu" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Boxes size={19} /></button><div className="suite-breadcrumb"><span>DNS</span><ChevronRight size={14} /><strong>{TITLES[page]}</strong></div><div className="suite-top-actions"><button className="suite-icon-button" onClick={() => setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system')} aria-label={`主题：${theme === 'system' ? '系统' : theme === 'light' ? '明亮' : '深色'}，点击切换`} title={`主题：${theme === 'system' ? '系统' : theme === 'light' ? '明亮' : '深色'}`}>{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button><button className="suite-refresh" onClick={() => void refresh()} disabled={refreshing} aria-busy={refreshing} aria-label="刷新状态"><Activity size={16} className={refreshing ? 'spin' : ''} /><span>刷新</span></button><details className="suite-account"><summary><KeyRound size={16} />账户</summary><div className="suite-account-menu"><button className="btn btn-secondary btn-sm" onClick={onPassword}><KeyRound size={15} />修改管理员密码</button><button className="btn btn-secondary btn-sm" onClick={onLogout}><LogOut size={15} />退出登录</button></div></details></div></header>
      <section className="suite-content"><div className="suite-page-heading"><div><span className="suite-kicker">LOVESTDNS</span><h1>{TITLES[page]}</h1></div><div className="suite-page-meta"><span><Server size={15} /> {caps?.deployment === 'native' ? '原生服务' : caps?.deployment === 'container' ? '容器服务' : '等待实例'}</span><Status value={health} label={error ? '连接中断 · 状态待确认' : paused ? '采样已暂停 · 显示历史快照' : runtimeStale ? '数据过期 · 状态待确认' : runtime?.core_healthy ? '运行中' : runtime ? 'DNS 状态需检查' : '等待状态采样'} /></div></div>
        {(error || actionError) && <div className="suite-global-error"><HeartPulse size={16} /><div><strong>{error ? '连接暂时中断，草稿已保留' : '操作未完成'}</strong><p>{error || actionError}</p></div><button className="btn btn-secondary btn-sm" onClick={() => void refresh()}>重试</button></div>}
        {runtimeError && !error && <div className="suite-global-warning"><Activity size={16} /><span>运行采集暂不可用：{runtimeError}</span></div>}
        {['config', 'env', 'lists', 'advanced', 'cache', 'ttl'].includes(page) && <div className="suite-page-tabs" aria-label="页面分区">{(navPage(page) === 'cache' ? [['cache', '缓存统计与快照'], ['ttl', 'TTL 规则']] : [['config', '配置状态'], ['env', '基础与上游'], ['lists', '域名分流'], ['advanced', '高级配置']]).map(([key, label]) => <a key={key} aria-current={page === key ? 'page' : undefined} href={`#${PATHS[key as Page]}`}>{label}</a>)}</div>}
        {['env', 'lists', 'advanced', 'ttl'].includes(page) && caps?.permissions.configure && <ConfigState compact />}
        <PageBoundary key={`${page}-${pageAttempt}`} onRetry={() => { retryPage(page); setPageAttempt((value) => value + 1); }}><Suspense fallback={<p role="status">正在加载页面…</p>}>
        {['env', 'lists', 'ttl', 'advanced'].includes(page) ? caps ? <Permission allowed={caps.permissions.configure}><Active key={`${page}-${caps.instance_id}-${caps.access_role}`} /></Permission> : <Offline /> : <Active key={`${caps?.instance_id}-${caps?.access_role}`} />}
        </Suspense></PageBoundary>
      </section>
    </main>
  </div>;
}

export default function App() {
  useUnsavedChanges(false);
  const [session, setSession] = useState<BrowserSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState('');
  const [actionError, setActionError] = useState('');
  const [changing, setChanging] = useState(false);
  const [theme, setTheme] = useState<Theme>(() => {
    try { const saved = localStorage.getItem('paopaodns-theme'); return saved === 'dark' || saved === 'light' ? saved : 'system'; } catch { return 'system'; }
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
  useEffect(() => { const media = window.matchMedia('(prefers-color-scheme: dark)'); const update = () => { document.documentElement.dataset.theme = theme === 'system' ? media.matches ? 'dark' : 'light' : theme; }; update(); media.addEventListener('change', update); try { localStorage.setItem('paopaodns-theme', theme); } catch { /* ignore */ } return () => media.removeEventListener('change', update); }, [theme]);
  const logout = async () => {
    if (!confirmDiscard()) return;
    setActionError('');
    try { const value = await browserAuth('logout', {}); clearFileDrafts(); clearMaintenanceRequests(); useStore.getState().resetAll(); setSession(value); setChanging(false); }
    catch (e) { setActionError(errorText(e)); }
  };
  if (!session?.authenticated || session.must_change_password || changing) return <BrowserLogin key={changing || session?.must_change_password ? 'password' : 'login'} session={session} loading={loading} error={authError} changePassword={changing} onAuthenticated={(value) => { setSession(value); setChanging(false); setActionError(''); }} onCancel={() => setChanging(false)} onRetry={() => { setLoading(true); void checkSession(); }} />;
  return <SuiteProvider><GatewayProvider><Shell theme={theme} setTheme={setTheme} onLogout={() => void logout()} onPassword={() => setChanging(true)} actionError={actionError} /></GatewayProvider></SuiteProvider>;
}
