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
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可读取和编辑配置。</Empty>;
  return <div className="suite-page"><Permission allowed={caps.permissions.configure}>
    <Notice>编辑目标配置，保存后按加载方式处理，再执行样本查询。<a href="#/system?tab=apply">配置历史与维护日志</a></Notice>
    <div className="suite-config-grid">{cards.map(({ href, title, desc, icon: Icon }) => <a className="suite-config-card" href={href} key={href}><span><Icon size={20} /></span><div><strong>{title}</strong><p>{desc}</p></div><ArrowRight size={17} /></a>)}</div>
    <ConfigState />
    {caps.permissions.maintain && <div className="suite-config-workflow"><div className="suite-workflow-steps"><span>1 · 保存目标</span><span>2 · 应用 / 重启</span><span>3 · 样本查询</span></div><SuiteMaintenance caps={caps} runtime={runtime} mode="config" /><ConfigVerification /></div>}
  </Permission></div>;
}
