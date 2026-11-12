import { useId } from 'react';
import type { TrendSample } from '../trend';
import { trendSegments } from '../trend';

export default function TrendChart({ samples, field, label, unit, color = 'var(--accent-blue)', emptyLabel = '等待有效采样' }: {
  samples: TrendSample[]; field: Exclude<keyof TrendSample, 'at'>; label: string; unit: string; color?: string; emptyLabel?: string;
}) {
  const id = useId();
  const values = samples.map((point) => point[field]).filter((v): v is number => v !== null);
  const ratio = field.endsWith('Hit');
  const max = ratio ? 1 : Math.max(...values, 1) * 1.15;
  const start = samples[0]?.at ?? 0;
  const end = Math.max(samples.at(-1)?.at ?? 0, start + 30);
  const x = (at: number) => 48 + (at - start) / (end - start) * 330;
  const y = (v: number) => 150 - v / max * 122;
  const format = (v: number) => ratio ? `${Math.round(v * 100)}%` : field === 'memory' ? `${(v / 1048576).toFixed(1)}` : v.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  const segments = trendSegments(samples, field);
  const stamp = (value: number) => new Date(value * 1000).toLocaleTimeString('zh-CN', { hour12: false });
  return <div className="suite-chart"><div className="suite-chart-caption"><strong>{label}</strong><span>{unit}</span></div>
    <svg viewBox="0 0 390 192" role="img" aria-labelledby={id}>
      <title id={id}>{label}，{values.length ? `当前会话 ${values.length} 个有效样本` : '暂无有效样本'}</title>
      {[0, .5, 1].map((step) => <g key={step}><line x1="48" x2="378" y1={y(step * max)} y2={y(step * max)} className="suite-chart-grid" /><text x="38" y={y(step * max) + 4} textAnchor="end">{format(step * max)}</text></g>)}
      {segments.map((part, index) => <g key={index}><polyline points={part.map((p) => `${x(p.at)},${y(p[field]!)}`).join(' ')} fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        {part.length === 1 && <circle cx={x(part[0].at)} cy={y(part[0][field]!)} r="3" fill={color} />}</g>)}
      {!values.length && <text x="213" y="90" textAnchor="middle" className="suite-chart-placeholder">{emptyLabel}</text>}
      {!!samples.length && <><text x="48" y="180">{stamp(start)}</text><text x="378" y="180" textAnchor="end">{stamp(end)}</text></>}
    </svg></div>;
}
