import type { ReactNode } from 'react';
import { AlertCircle, Database, Radio, Server, ShieldCheck } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export function Empty({ title, children, icon: Icon = Database }: { title: string; children?: ReactNode; icon?: LucideIcon }) {
  return <div className="suite-empty"><span className="suite-empty-icon"><Icon size={23} /></span><strong>{title}</strong>{children && <p>{children}</p>}</div>;
}
export function Notice({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warning' | 'error' }) {
  return <div className={`suite-notice is-${tone}`} role={tone === 'error' ? 'alert' : 'status'}><AlertCircle size={16} /><div>{children}</div></div>;
}
export function Status({ value, label }: { value: 'good' | 'warning' | 'error' | 'neutral'; label: string }) {
  return <span className={`suite-status is-${value}`}><i />{label}</span>;
}
export function Section({ title, subtitle, icon: Icon, actions, children }: { title: string; subtitle?: string; icon?: LucideIcon; actions?: ReactNode; children: ReactNode }) {
  return <section className="card suite-section"><header><div>{Icon && <Icon size={18} />}<div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div></div>{actions}</header>{children}</section>;
}
export function Stat({ label, value, detail, icon: Icon = Server }: { label: string; value: ReactNode; detail: string; icon?: LucideIcon }) {
  return <div className="card suite-stat"><div><span>{label}</span><Icon size={18} /></div><strong>{value}</strong><small>{detail}</small></div>;
}
export function Permission({ kind = 'configure', allowed, children }: { kind?: 'configure' | 'maintain'; allowed: boolean; children: ReactNode }) {
  return allowed ? <>{children}</> : <Empty icon={ShieldCheck} title="需要管理权限">当前会话无法{kind === 'configure' ? '编辑配置' : '执行维护'}，请重新登录管理员账号。</Empty>;
}
export function Offline() { return <Empty icon={Radio} title="等待 DNS 服务响应">请检查服务状态，或稍后点击刷新重试。</Empty>; }
