import { Activity, Database, FileCheck, HeartPulse, Settings2, ShieldCheck } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { bytes } from '../suiteApi';
import { Section, Stat, Status, Notice, Offline } from '../components/SuiteUI';
import TrendChart from '../components/TrendChart';
import './SuitePage.css';

export default function SuiteDashboard() {
  const { caps, runtime, samples } = useSuite();
  if (!caps) return <Offline />;
  const components = Object.entries(runtime?.components ?? {});
  const healthy = runtime?.core_healthy === true;
  const activeLayers = Object.values(runtime?.layers ?? {}).filter((layer) => layer.available).length;
  return <div className="suite-page">
    <div className="suite-welcome"><div><span className="suite-kicker">DNS SERVICE</span><h2>DNS 服务状态与配置</h2><p>集中查看核心健康、DNS 分层指标和配置状态；需要排查问题时，从诊断页开始。</p></div><div className="suite-welcome-mark"><Activity size={42} /></div></div>
    {!healthy && <Notice tone="warning">{runtime ? '核心组件状态需要检查。' : '尚未取得有效运行快照。'}请查看监控页的组件状态和采集信息。</Notice>}
    <div className="suite-stat-grid"><Stat label="核心服务" value={<Status value={healthy ? 'good' : runtime ? 'warning' : 'neutral'} label={healthy ? '运行正常' : runtime ? '需要检查' : '等待采样'} />} detail={runtime ? `${components.filter(([, value]) => value.statename === 'RUNNING').length}/${components.length} 个组件运行中` : '等待运行快照'} icon={HeartPulse} /><Stat label="DNS 观测层" value={`${activeLayers}/2`} detail="递归侧与转发侧 Unbound" icon={Database} /><Stat label="Redis 内存" value={bytes(runtime?.redis?.info?.used_memory)} detail="来源：Redis INFO" icon={Database} /><Stat label="当前权限" value={caps.access_role === 'admin' ? '管理' : '只读'} detail={caps.permissions.configure ? '可修改配置和维护' : '只能观测和诊断'} icon={ShieldCheck} /></div>
    <div className="suite-dashboard-grid"><Section title="最近采样趋势" subtitle="仅保留当前页面会话样本，缺失值不会被补成 0" icon={Activity}><div className="suite-chart-pair"><TrendChart samples={samples} field="recursive" label="递归侧 QPS" unit="queries / second" /><TrendChart samples={samples} field="recursiveHit" label="递归侧缓存命中率" unit="ratio" color="var(--accent-teal)" /></div></Section><Section title="下一步操作" subtitle="按常见排查顺序组织" icon={Settings2}><div className="suite-action-list"><a href="#/diagnostics"><div><HeartPulse size={18} /><span><strong>检查一个域名</strong><small>查看记录、TTL、RCODE 和诊断标记</small></span></div><span>→</span></a><a href="#/monitoring"><div><Activity size={18} /><span><strong>查看运行指标</strong><small>确认组件、采集器和两路 Unbound</small></span></div><span>→</span></a><a href="#/config"><div><FileCheck size={18} /><span><strong>进入配置中心</strong><small>查看文件、历史和应用前置条件</small></span></div><span>→</span></a></div></Section></div>
    <Section title="组件概况" subtitle="状态来自 Supervisor 快照；不会把 DNS 查询成功当作组件健康" icon={Database}><div className="suite-component-grid">{components.length ? components.map(([name, item]) => <div className="suite-component" key={name}><i className={item.statename === 'RUNNING' ? 'is-good' : 'is-warning'} /><code>{name}</code><span>{item.statename}</span></div>) : <p className="suite-muted">暂无组件快照</p>}</div></Section>
  </div>;
}
