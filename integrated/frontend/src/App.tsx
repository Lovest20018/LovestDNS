import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { LucideIcon } from 'lucide-react';
import {
  Activity, Boxes, ChevronRight, FileCog, HeartPulse, KeyRound,
  LogOut, Moon, Network, Pause, Play, ScrollText, Shield, Sun, X,
} from 'lucide-react';
import { useFilterView } from './filterContext';
import { FilterProvider } from './FilterProvider';
import BrowserLogin from './components/BrowserLogin';
import SuiteProvider from './SuiteProvider';
import MaintenanceProvider from './MaintenanceProvider';
import UpdateNetworkProvider from './UpdateNetworkProvider';
import { isActiveTask, useMaintenance } from './maintenanceContext';
import GatewayProvider from './GatewayProvider';
import { useSuite } from './suiteContext';
import { browserAuth, errorText, setBrowserCsrf } from './suiteApi';
import type { BrowserSession } from './suiteApi';
import { Status, Permission, Offline } from './components/SuiteUI';
import { confirmDiscard, useUnsavedChanges } from './unsaved';
import { clearFileDrafts } from './fileDrafts';
import { useStore } from './store';
import { clearMaintenanceRequests } from './maintenanceRequests';
import PageBoundary from './components/PageBoundary';
import { clearDiagnosticRoutes } from './useDiagnosticRoute';
import { clearSourceDevices } from './sourceDevices';
import './suite.css';

type Page = 'cache' | 'dashboard' | 'diagnostics' | 'config' | 'env' | 'lists' | 'ttl' | 'advanced' | 'filter' | 'system' | 'deploy';
const PATHS: Record<Page, string> = {
  cache: '/cache', config: '/config', dashboard: '/', diagnostics: '/diagnostics',
  env: '/env', lists: '/lists', ttl: '/ttl', advanced: '/advanced', filter: '/filter', system: '/system', deploy: '/deploy',
};
const TITLES: Record<Page, string> = {
  cache: '缓存与快照', config: '配置状态与应用', dashboard: '概览', diagnostics: '查询与诊断',
  env: '运行参数', lists: '域名分流', ttl: 'TTL 规则', advanced: '高级配置文件', filter: '广告过滤', system: '维护', deploy: '系统设置',
};
const LOADERS = {
  cache: () => import('./pages/SuiteCache'), dashboard: () => import('./pages/SuiteDashboard'), diagnostics: () => import('./pages/SuiteDiagnostics'),
  config: () => import('./pages/SuiteConfig'), system: () => import('./pages/SuiteSystem'), deploy: () => import('./pages/SuiteDeploy'),
  env: () => import('./pages/EnvConfigPage'), lists: () => import('./pages/DomainListPage'), ttl: () => import('./pages/TtlRulesPage'), advanced: () => import('./pages/AdvancedConfigPage'),
  filter: () => import('./pages/SuiteFiltering'),
};
const COMPONENTS = {
  cache: lazy(LOADERS.cache), dashboard: lazy(LOADERS.dashboard), diagnostics: lazy(LOADERS.diagnostics), config: lazy(LOADERS.config),
  system: lazy(LOADERS.system), deploy: lazy(LOADERS.deploy), env: lazy(LOADERS.env),
  lists: lazy(LOADERS.lists), ttl: lazy(LOADERS.ttl), advanced: lazy(LOADERS.advanced), filter: lazy(LOADERS.filter),
};
function retryPage(page: Page) { COMPONENTS[page] = lazy(LOADERS[page]); }
const NAV: { title: string; items: { key: Page; icon: LucideIcon }[] }[] = [
  { title: 'DNS 控制台', items: [{ key: 'dashboard', icon: Activity }, { key: 'lists', icon: FileCog }, { key: 'diagnostics', icon: HeartPulse }, { key: 'filter', icon: Shield }, { key: 'system', icon: ScrollText }, { key: 'deploy', icon: Network }] },
];
function navPage(page: Page): Page {
  if (page === 'cache') return 'system';
  return ['config', 'ttl', 'env', 'advanced'].includes(page) ? 'deploy' : page;
}

const DNS_SETTINGS = [
  { page: 'env', href: '#/settings?tab=runtime', label: TITLES.env },
  { page: 'config', href: '#/config', label: TITLES.config },
  { page: 'ttl', href: '#/ttl', label: TITLES.ttl },
  { page: 'advanced', href: '#/advanced', label: TITLES.advanced },
];
const INSTANCE_SETTINGS = [
  { tab: 'updates', label: '软件更新', maintain: true },
  { tab: 'service', label: '服务信息' },
  { tab: 'external', label: '外部接入', configure: true },
  { tab: 'devices', label: '来源设备', configure: true },
];

function routeFromHash(hash: string) {
  const [path = '/', query] = hash.replace(/^#/, '').split('?');
  const params = new URLSearchParams(query);
  let page = (Object.entries(PATHS).find(([, value]) => value === (path || '/'))?.[0] as Page | undefined) ?? 'dashboard';
  let tab = params.get('tab') ?? '';
  let canonical = '';
  if (path === '/settings' || path === '/deploy') {
    page = path === '/settings' && tab === 'runtime' ? 'env' : 'deploy';
    if (tab === 'gateway') tab = 'external';
    if (!INSTANCE_SETTINGS.some((item) => item.tab === tab) && page === 'deploy') tab = 'service';
  }
  if (path === '/gateway') {
    page = 'diagnostics';
    tab = 'gateway';
    params.set('tab', 'gateway');
    canonical = `#/diagnostics?${params}`;
  }
  if (path === '/system' && tab === 'updates' && !params.has('component')) {
    page = 'deploy';
    canonical = `#/settings?${params}`;
  }
  if (path === '/system' && tab === 'apply' && !params.has('component')) page = 'config';
  if (['/system', '/status', '/monitoring', '/cache'].includes(path) && page !== 'deploy' && page !== 'config') {
    tab = params.has('component') ? 'logs' : path === '/cache' ? 'cache' : tab;
    page = tab === 'cache' ? 'cache' : 'system';
  }
  return { page, tab, canonical };
}

function sectionTitle(page: Page, tab: string) {
  if (page === 'deploy') return INSTANCE_SETTINGS.find((item) => item.tab === tab)?.label ?? '服务信息';
  if (page === 'system') return tab === 'logs' ? '组件日志' : tab === 'history' ? '配置历史' : '运行状态';
  if (page === 'diagnostics') return ({ gateway: '网关观测', records: '查询记录', compare: '链路对照' })[tab] ?? '主动查询';
  if (page === 'filter') return ({ rules: '订阅与规则', allow: '放行列表' })[tab] ?? '拦截记录';
  return TITLES[page];
}

function replaceHash(hash: string) {
  if (location.hash !== hash) history.replaceState(history.state, '', hash);
}

type Theme = 'light' | 'dark' | 'system';

function Console(props: Parameters<typeof Shell>[0]) {
  const { caps } = useSuite();
  return <MaintenanceProvider key={caps?.instance_id ?? ''} instance={caps?.instance_id} allowed={caps?.permissions.maintain === true}>
    <FilterProvider key={`${caps?.instance_id}-${caps?.access_role}`}><UpdateNetworkProvider><GatewayProvider><Shell {...props} /></GatewayProvider></UpdateNetworkProvider></FilterProvider>
  </MaintenanceProvider>;
}

function Shell({ theme, setTheme, onLogout, onPassword, actionError }: { theme: Theme; setTheme: (value: Theme) => void; onLogout: () => void; onPassword: () => void; actionError: string }) {
  const { caps, error, runtimeError, paused, setPaused, loading, refreshing, refresh } = useSuite();
  const { tasks } = useMaintenance();
  const filter = useFilterView();
  const active = tasks.find(isActiveTask);
  const [hash, setHash] = useState(() => location.hash);
  const { page, tab, canonical } = routeFromHash(hash);
  const title = sectionTitle(page, tab);
  const primaryPage = navPage(page);
  const heading = useRef<HTMLHeadingElement>(null);
  const focusHeading = useRef(false);
  useEffect(() => {
    if (!hash) replaceHash('#/');
    if (canonical) {
      replaceHash(canonical);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    }
  }, [canonical, hash]);

  const [mobileOpen, setMobileOpen] = useState(false);
  useEffect(() => {
    if (!mobileOpen && focusHeading.current) {
      heading.current?.focus({ preventScroll: true });
      focusHeading.current = false;
    }
  }, [mobileOpen]);
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
    return () => { document.removeEventListener('keydown', keydown); if (!focusHeading.current) (opener ?? previous)?.focus(); };
  }, [mobileOpen]);
  useEffect(() => {
    const sync = (event: HashChangeEvent) => {
      setHash(location.hash);
      setMobileOpen(false);
      const section = (url: string) => {
        const { page, tab } = routeFromHash(url.slice(url.indexOf('#')));
        return `${page}:${tab}`;
      };
      if (event.oldURL && section(event.oldURL) !== section(event.newURL)) window.scrollTo(0, 0);
    };
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  return <div className="suite-app">
    <div className={`suite-sidebar-backdrop ${mobileOpen ? 'is-open' : ''}`} onClick={() => setMobileOpen(false)} />
    <aside inert={narrow && !mobileOpen} ref={sidebar} id="suite-navigation" aria-label="主导航" className={`suite-sidebar ${mobileOpen ? 'is-open' : ''}`}>
      <a href="#/" className="suite-brand"><img className="suite-brand-mark" src="/lovestdns-icon.svg" alt="" /><div><strong>LovestDNS</strong><span>DNS 管理面板</span></div></a>
      <button className="suite-mobile-close" onClick={() => setMobileOpen(false)} aria-label="关闭导航"><X size={18} /></button>
      <a href="#/status" className="suite-instance"><div className="suite-instance-icon"><Network size={17} /></div><div><small>当前 DNS</small><strong title={caps?.name ?? 'LovestDNS'}>{caps?.name ?? 'LovestDNS'}</strong></div><Status value={error ? 'warning' : 'neutral'} label={error ? '失联' : active ? '维护中' : loading ? '连接中' : caps?.deployment === 'native' ? '原生' : caps ? '容器' : '等待连接'} /></a>
      <nav className="suite-nav" onClick={(event) => {
        if (!mobileOpen || !(event.target instanceof Element && event.target.closest('a')) || event.defaultPrevented) return;
        focusHeading.current = true;
        window.scrollTo(0, 0);
        setMobileOpen(false);
      }}>{NAV.map((group) => <div className="suite-nav-group" key={group.title}>{group.items.map((item) => { if (item.key === 'lists' && caps && !caps.permissions.configure) return null; if (item.key === 'filter' && !(caps?.features.dns_filter && caps.permissions.configure)) return null; const Icon = item.icon; return <a key={item.key} href={item.key === 'deploy' ? caps?.permissions.configure ? '#/settings?tab=runtime' : '#/settings?tab=service' : `#${PATHS[item.key]}`} aria-current={navPage(page) === item.key ? 'page' : undefined} className={navPage(page) === item.key ? 'is-active' : ''}><Icon size={17} /><span>{TITLES[item.key]}</span>{item.key === 'filter' && filter.state?.draft && <small>待复核</small>}{navPage(page) === item.key && <ChevronRight size={14} />}</a>; })}</div>)}</nav>
      <div className="suite-sidebar-footer"><span>本地配置与诊断</span><small>{caps?.deployment === 'native' ? '原生服务' : caps ? '容器服务' : 'LovestDNS'} · {caps?.version?.replace('0.1.0-dev.lovestdns-native.', '') ?? '准备中'}</small></div>
    </aside>
    <main className="suite-main" inert={mobileOpen}>
      <header className="suite-topbar"><button ref={menuButton} aria-controls="suite-navigation" aria-expanded={mobileOpen} className="suite-mobile-menu" onClick={() => setMobileOpen(true)} aria-label="打开导航"><Boxes size={19} /></button><div className="suite-breadcrumb">{title !== TITLES[primaryPage] && <><span>{TITLES[primaryPage]}</span><ChevronRight size={14} /></>}<strong>{title}</strong></div><div className="suite-top-actions"><button className="suite-icon-button" onClick={() => setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system')} aria-label={`主题：${theme === 'system' ? '系统' : theme === 'light' ? '明亮' : '深色'}，点击切换`} title={`主题：${theme === 'system' ? '系统' : theme === 'light' ? '明亮' : '深色'}`}>{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button><div className="suite-instance-refresh" role="group" aria-label="实例状态刷新"><button className="suite-refresh" onClick={() => void refresh()} disabled={refreshing} aria-busy={refreshing} aria-label="刷新状态"><Activity size={16} className={refreshing ? 'spin' : ''} /><span>状态刷新</span></button><button className="suite-refresh suite-refresh-toggle" onClick={() => setPaused(!paused)} aria-label={paused ? '恢复自动刷新' : '暂停自动刷新'} title={paused ? '恢复自动刷新' : '暂停自动刷新'}>{paused ? <Play size={16} /> : <Pause size={16} />}</button></div><details className="suite-account"><summary><KeyRound size={16} />账户</summary><div className="suite-account-menu"><button className="btn btn-secondary btn-sm" onClick={onPassword}><KeyRound size={15} />修改管理员密码</button><button className="btn btn-secondary btn-sm" onClick={onLogout}><LogOut size={15} />退出登录</button></div></details></div></header>
      <section className="suite-content"><div className="suite-page-heading"><h1 ref={heading} tabIndex={-1}>{title}</h1></div>
        {(error || actionError) && <div className="suite-global-error"><HeartPulse size={16} /><div><strong>{error ? '连接暂时中断，草稿已保留' : '操作未完成'}</strong><p>{error || actionError}</p></div><button className="btn btn-secondary btn-sm" onClick={() => void refresh()}>重试</button></div>}
        {runtimeError && !error && !active && page !== 'dashboard' && <div className="suite-global-warning"><Activity size={16} /><span>运行状态读取暂不可用。{runtimeError}</span></div>}
        {paused && <div className="suite-refresh-paused" role="status"><div><strong>自动刷新已暂停</strong><p>页面保留上次结果。进行中的维护任务会继续跟踪，DNS 查询记录沿用当前开关。</p></div></div>}
        {primaryPage === 'deploy' && caps && <nav className="suite-settings-nav" aria-label="系统设置分区">
          {caps.permissions.configure && <section aria-label="DNS 配置"><strong>DNS 配置</strong><div>{DNS_SETTINGS.map((item) => <a key={item.page} href={item.href} aria-current={page === item.page ? 'page' : undefined}>{item.label}</a>)}</div></section>}
          <section aria-label="实例管理"><strong>实例管理</strong><div>{INSTANCE_SETTINGS.filter((item) => (!item.maintain || caps.permissions.maintain || caps.deployment === 'container') && (!item.configure || caps.permissions.configure)).map((item) => <a key={item.tab} href={`#/settings?tab=${item.tab}`} aria-current={page === 'deploy' && tab === item.tab ? 'page' : undefined}>{item.label}</a>)}</div></section>
        </nav>}
        {primaryPage === 'system' && caps && <nav className="suite-page-tabs" aria-label="维护分区">
          {[['status', '运行状态'], ...(caps.permissions.maintain ? [['logs', '组件日志']] : []), ['cache', '缓存与快照'], ...(caps.permissions.maintain ? [['history', '配置历史']] : [])].map(([key, label]) => <a key={key} href={`#/system?tab=${key}`} aria-current={(page === 'cache' ? 'cache' : ['logs', 'history'].includes(tab) ? tab : 'status') === key ? 'page' : undefined}>{label}</a>)}
        </nav>}
        <PageBoundary key={`${page}-${pageAttempt}`} onRetry={() => { retryPage(page); setPageAttempt((value) => value + 1); }}><Suspense fallback={<p role="status">正在加载页面…</p>}>
        {['env', 'lists', 'ttl', 'advanced', 'filter'].includes(page) ? caps ? <Permission allowed={caps.permissions.configure && (page !== 'filter' || !!caps.features.dns_filter)}><Active key={`${page}-${caps.instance_id}-${caps.access_role}`} /></Permission> : <Offline /> : <Active key={`${caps?.instance_id}-${caps?.access_role}`} />}
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
    const expired = () => { clearDiagnosticRoutes(); clearSourceDevices(); setBrowserCsrf(''); setChanging(false); setLoading(true); void checkSession(); };
    window.addEventListener('lovestdns-auth-expired', expired);
    return () => { cancelled = true; window.removeEventListener('lovestdns-auth-expired', expired); };
  }, [checkSession]);
  useEffect(() => { const media = window.matchMedia('(prefers-color-scheme: dark)'); const update = () => { document.documentElement.dataset.theme = theme === 'system' ? media.matches ? 'dark' : 'light' : theme; }; update(); media.addEventListener('change', update); try { localStorage.setItem('paopaodns-theme', theme); } catch { /* ignore */ } return () => media.removeEventListener('change', update); }, [theme]);
  const logout = async () => {
    if (!confirmDiscard()) return;
    setActionError('');
    try { const value = await browserAuth('logout', {}); clearFileDrafts(); clearMaintenanceRequests(); clearDiagnosticRoutes(); clearSourceDevices(); useStore.getState().resetAll(); setSession(value); setChanging(false); }
    catch (e) { setActionError(errorText(e)); }
  };
  if (!session?.authenticated || session.must_change_password || changing) return <BrowserLogin key={changing || session?.must_change_password ? 'password' : 'login'} session={session} loading={loading} error={authError} changePassword={changing} onAuthenticated={(value) => { setSession(value); setChanging(false); setActionError(''); }} onCancel={() => setChanging(false)} onRetry={() => { setLoading(true); void checkSession(); }} />;
  return <SuiteProvider><Console theme={theme} setTheme={setTheme} onLogout={() => void logout()} onPassword={() => { if (confirmDiscard()) setChanging(true); }} actionError={actionError} /></SuiteProvider>;
}
