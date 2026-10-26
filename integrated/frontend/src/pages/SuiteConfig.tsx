import { ArrowRight, Clock3, FileCog, FileText, Settings2 } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { Empty, Notice, Permission } from '../components/SuiteUI';
import SuiteHistory from '../components/SuiteHistory';
import SuiteMaintenance from '../components/SuiteMaintenance';
import './SuitePage.css';

const cards = [
  { href: '#/env', title: '环境变量', desc: '查看运行时覆盖和生效条件', icon: Settings2 },
  { href: '#/lists', title: '域名列表', desc: '维护分流、递归和 DNSCrypt 列表', icon: FileText },
  { href: '#/ttl', title: 'TTL 规则', desc: '管理域名与响应 TTL 规则', icon: Clock3 },
  { href: '#/advanced', title: '高级配置', desc: '编辑 custom_env、custom_mod 与 Unbound', icon: FileCog },
];
export default function SuiteConfig() {
  const { caps, runtime } = useSuite();
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可读取和编辑配置。</Empty>;
  return <div className="suite-page"><Permission allowed={caps.permissions.configure}>
    <Notice>保存、恢复和应用分别记录结果。请按文件返回的生效条件处理，并查询目标域名验证规则。</Notice>
    <div className="suite-config-grid">{cards.map(({ href, title, desc, icon: Icon }) => <a className="suite-config-card" href={href} key={href}><span><Icon size={20} /></span><div><strong>{title}</strong><p>{desc}</p></div><ArrowRight size={17} /></a>)}</div>
    <SuiteHistory />
    <SuiteMaintenance caps={caps} runtime={runtime} mode="config" />
  </Permission></div>;
}
