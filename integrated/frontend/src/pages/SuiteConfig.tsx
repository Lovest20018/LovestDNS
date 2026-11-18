import { useCallback, useEffect, useRef } from 'react';
import { ArrowRight, Clock3, FileCog, FileText, Settings2 } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { Empty, Notice, Permission } from '../components/SuiteUI';
import ConfigState from '../components/ConfigState';
import SuiteMaintenance from '../components/SuiteMaintenance';
import ConfigVerification from '../components/ConfigVerification';
import './SuitePage.css';
import './HomeWorkflow.css';

const cards = [
  { href: '#/env', title: '基础设置', desc: '调整 DNS 参数与运行时覆盖', icon: Settings2 },
  { href: '#/lists', title: '域名分流', desc: '维护转发、递归和 DNSCrypt 列表', icon: FileText },
  { href: '#/ttl', title: 'TTL 规则', desc: '管理域名与响应 TTL 规则', icon: Clock3 },
  { href: '#/advanced', title: '高级配置', desc: '编辑 custom_env、custom_mod 与 Unbound', icon: FileCog },
];
export default function SuiteConfig() {
  const { caps, runtime } = useSuite();
  const canMaintain = Boolean(caps?.permissions.configure && caps.permissions.maintain);
  const applyHeading = useRef<HTMLHeadingElement>(null);
  const focusApply = useCallback(() => {
    applyHeading.current?.focus({ preventScroll: true });
    applyHeading.current?.scrollIntoView({ block: 'start' });
  }, []);
  useEffect(() => {
    if (!caps?.permissions.configure) return;
    let frame = 0;
    const sync = () => {
      cancelAnimationFrame(frame);
      const [path, query] = window.location.hash.split('?');
      const params = new URLSearchParams(query);
      const applying = params.get('tab') === 'apply' && (path === '#/config' || (path === '#/system' && !params.has('component')));
      if (canMaintain && applying) frame = requestAnimationFrame(focusApply);
      if (path === '#/config' && params.get('tab') === 'changes') frame = requestAnimationFrame(() => {
        const changes = document.getElementById('config-changes');
        changes?.focus({ preventScroll: true });
        changes?.scrollIntoView({ block: 'start' });
      });
    };
    sync();
    window.addEventListener('hashchange', sync);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('hashchange', sync);
    };
  }, [canMaintain, caps?.permissions.configure, focusApply]);
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可读取和编辑配置。</Empty>;
  return <div className="suite-page"><Permission allowed={caps.permissions.configure}>
    <Notice>保存配置后可在编辑处继续操作。{canMaintain && <a href="#/config?tab=apply" onClick={() => {
      if (window.location.hash === '#/config?tab=apply') focusApply();
    }}>前往应用配置</a>}<a href="#/system?tab=history">配置历史与恢复</a></Notice>
    <div className="suite-config-grid">{cards.map(({ href, title, desc, icon: Icon }) => <a className="suite-config-card" href={href} key={href}><span><Icon size={20} /></span><div><strong>{title}</strong><p>{desc}</p></div><ArrowRight size={17} /></a>)}</div>
    <ConfigState />
    {canMaintain && <section className="suite-config-workflow" aria-labelledby="config-apply">
      <h2 id="config-apply" ref={applyHeading} tabIndex={-1}>应用配置</h2>
      <SuiteMaintenance caps={caps} runtime={runtime} mode="config" />
      <ConfigVerification />
    </section>}
  </Permission></div>;
}
