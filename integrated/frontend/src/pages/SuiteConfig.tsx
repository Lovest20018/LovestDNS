import { useState } from 'react';
import { ArrowRight, Clock3, FileCog, FileText, Settings2 } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { Empty, Notice, Permission } from '../components/SuiteUI';
import ConfigState from '../components/ConfigState';
import SuiteHistory from '../components/SuiteHistory';
import SuiteMaintenance from '../components/SuiteMaintenance';
import './SuitePage.css';

const cards = [
  { href: '#/env', title: '基础设置', desc: '调整 DNS 参数与运行时覆盖', icon: Settings2 },
  { href: '#/lists', title: '域名分流', desc: '维护转发、递归和 DNSCrypt 列表', icon: FileText },
  { href: '#/ttl', title: 'TTL 规则', desc: '管理域名与响应 TTL 规则', icon: Clock3 },
  { href: '#/advanced', title: '高级配置', desc: '编辑 custom_env、custom_mod 与 Unbound', icon: FileCog },
];
export default function SuiteConfig() {
  const [historyOpen, setHistoryOpen] = useState(false);
  const { caps, runtime } = useSuite();
  if (!caps) return <Empty title="等待 DNS 实例连接">连接后可读取和编辑配置。</Empty>;
  return <div className="suite-page"><Permission allowed={caps.permissions.configure}>
    <Notice>文件保存、配置应用与域名验证是三个步骤。{caps.deployment === 'native' ? '基础参数与 Unbound 配置保存后重启 DNS 核心。' : '基础参数与 Unbound 配置保存后，按服务信息中的主机说明应用。'}自定义规则可先预览再应用。</Notice>
    <div className="suite-config-stage"><span>1</span><div><h2>编辑与保存</h2><p>选择配置类别，保存后按结果提示继续。</p></div></div>
    <div className="suite-config-grid">{cards.map(({ href, title, desc, icon: Icon }) => <a className="suite-config-card" href={href} key={href}><span><Icon size={20} /></span><div><strong>{title}</strong><p>{desc}</p></div><ArrowRight size={17} /></a>)}</div>
    <div className="suite-config-stage"><span>2</span><div><h2>应用配置</h2><p>选择适合文件的应用方式；任务完成后再查询目标域名验证。</p></div><a className="btn btn-secondary btn-sm" href="#/diagnostics">3 · 查询验证</a></div>
    <ConfigState />
    <SuiteMaintenance caps={caps} runtime={runtime} mode="config" />
    <details className="suite-config-history" onToggle={(e) => setHistoryOpen(e.currentTarget.open)}><summary><Clock3 size={16} />配置历史与恢复</summary>{historyOpen && <SuiteHistory />}</details>
  </Permission></div>;
}
