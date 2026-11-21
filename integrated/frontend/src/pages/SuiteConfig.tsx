import { useCallback, useEffect, useRef } from 'react';
import { useSuite } from '../suiteContext';
import { Empty, Permission } from '../components/SuiteUI';
import ConfigState from '../components/ConfigState';
import SuiteMaintenance from '../components/SuiteMaintenance';
import ConfigVerification from '../components/ConfigVerification';
import './SuitePage.css';
import './HomeWorkflow.css';

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
    <nav className="suite-local-links" aria-label="配置操作">{canMaintain && <a className="btn btn-secondary btn-sm" href="#/config?tab=apply" onClick={() => {
      if (window.location.hash === '#/config?tab=apply') focusApply();
    }}>前往应用配置</a>}{caps.permissions.maintain && <a className="btn btn-secondary btn-sm" href="#/system?tab=history">配置历史与恢复</a>}</nav>
    <ConfigState />
    {canMaintain && <section className="suite-config-workflow" aria-labelledby="config-apply">
      <h2 id="config-apply" ref={applyHeading} tabIndex={-1}>应用配置</h2>
      <SuiteMaintenance caps={caps} runtime={runtime} mode="config" />
      <ConfigVerification />
    </section>}
  </Permission></div>;
}
