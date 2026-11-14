import { Activity, Database, Network } from 'lucide-react';
import { useSuite } from '../suiteContext';
import { useGateway } from '../gatewayContext';
import { bytes, when } from '../suiteApi';
import type { RuntimeSnapshot } from '../suiteApi';
import { Empty, Section, Status, Notice, Offline } from '../components/SuiteUI';
import { componentSummary, cacheSaveSummary } from '../runtimePresentation';
import { dnsAddress, entryListeners } from '../dnsListeners';
import ConfigState from '../components/ConfigState';
import TrendChart from '../components/TrendChart';
import './SuitePage.css';
import './HomeWorkflow.css';

const CORE = ['mosdns', 'unbound-raw', 'unbound-forward', 'dnscrypt', 'dnscrypt-socks', 'redis'];
export default function SuiteDashboard() {
  const { caps, runtime, samples, paused, runtimeStale, setPaused } = useSuite();
  const { config: gateway } = useGateway();
  if (!caps) return <Offline />;
  const layers = runtime?.layers ?? {};
  const components = runtime?.components ?? {};
  const auxiliary = Object.keys(components).filter((name) => !CORE.includes(name));
  const problems = auxiliary.filter((name) => ['warning', 'error'].includes(componentSummary(name, components[name]).tone));
  const cacheSave = cacheSaveSummary(runtime?.redis);
  const endpoints = entryListeners(runtime);
  const addresses = [...new Set(endpoints.map((endpoint) => dnsAddress(endpoint.address, endpoint.port)))];
  const historical = paused || runtimeStale;
  return <div className="suite-page suite-home">
    <div className="suite-toolbar"><p className="suite-muted">更新于 {when(runtime?.sampled_at)} · {paused ? '自动刷新已暂停' : runtimeStale ? '数据过期' : '每 5 秒刷新'}</p><button className="btn btn-secondary btn-sm" onClick={() => setPaused(!paused)}>{paused ? '恢复自动刷新' : '暂停自动刷新'}</button></div>
    {runtimeStale && !paused && <Notice>数据已过期 · 以下为历史快照。</Notice>}
    {runtime?.error ? <Notice tone="error">{runtime.error}</Notice> : runtime && !runtime.core_healthy ? <Notice tone="warning">核心组件需检查。<a href="#/system">查看组件日志</a></Notice> : null}
    {!!problems.length && <Notice tone="warning">辅助服务需检查：{problems.map((name, i) => <span key={name}>{i > 0 && '、'}<a href={`#/system?component=${encodeURIComponent(name)}`}>{name}</a></span>)}</Notice>}
    {caps.permissions.configure && <ConfigState compact />}
    <div className="suite-home-summary">
      <Section title="核心进程" icon={Activity}><strong className="suite-home-value">{!runtime ? '未采集' : historical ? '历史快照' : runtime.core_healthy ? '运行中' : '需检查'}</strong><p className="suite-note">{runtime ? `${CORE.filter((name) => components[name]?.statename === 'RUNNING').length} / ${CORE.length} 个进程运行` : '等待组件状态'} · 终端接入待确认</p><a href="#/diagnostics?tab=query">执行样本查询</a></Section>
      <Section title={caps.deployment === 'container' ? '容器内 DNS 监听' : 'DNS 进程监听'} icon={Network}><strong className="suite-home-value suite-home-address">{addresses.length ? addresses.join(' · ') : '未确认'}</strong><p className="suite-note">{endpoints.length ? `${[...new Set(endpoints.map((item) => item.transport.toUpperCase()))].join(' / ')} · ${runtime?.dns?.entry_component}` : '尚未取得入口进程的监听证据'}</p><details><summary>接入信息</summary><p className="suite-note">{endpoints.some((item) => item.wildcard) ? '通配监听地址不能填写为设备 DNS，请确认主机的局域网 IP。' : '监听记录来自进程 socket，不代表终端查询已经过此服务。'}{caps.deployment === 'container' && '宿主机映射端口需另行核对。'}</p><p className="suite-note">来源：Linux 进程监听 · 服务网络空间。<a href="#/deploy">版本与服务信息</a></p></details></Section>
      <Section title="Redis 缓存" icon={Database}><strong className="suite-home-value">{runtime?.redis?.available ? bytes(runtime.redis.info?.used_memory) : '未采集'}</strong><p className="suite-note">{runtime?.redis?.available ? runtime.redis.info?.maxmemory === 0 ? '未设置 Redis 上限' : `上限 ${bytes(runtime.redis.info?.maxmemory)}` : '等待缓存数据'}</p><p className="suite-note">{cacheSave.value}</p><a href="#/cache">查看缓存与持久化</a></Section>
    </div>
    <Section title="核心组件" subtitle="进程状态；停止不等于未启用" icon={Activity}><div className="suite-home-components">{CORE.map((name) => <Component key={name} name={name} runtime={runtime} />)}</div></Section>
    <Section title="解析通道" subtitle="按通道统计；不代表入口总量" icon={Activity}>
      <div className="suite-table-scroll"><table className="info-table"><thead><tr><th>通道</th><th>查询速率（QPS）</th><th>缓存命中率</th><th>采集状态</th></tr></thead><tbody>{([['recursive', '递归'], ['forward', '转发']] as const).map(([key, label]) => { const layer = layers[key]; return <tr key={key}><td>{label}</td><td>{layer?.available ? layer.qps ?? '—' : '—'}</td><td>{layer?.available && layer.cache_hit_ratio != null ? `${(layer.cache_hit_ratio * 100).toFixed(1)}%` : '—'}</td><td>{!layer?.available ? '不可用' : !layer.comparable ? '等待可比较样本' : '已采集'}</td></tr>; })}</tbody></table></div>
      <details className="suite-home-trends"><summary>查看查询与缓存趋势</summary><div className="suite-chart-pair"><TrendChart samples={samples} field="recursive" label="递归 QPS" unit="次 / 秒" /><TrendChart samples={samples} field="forward" label="转发 QPS" unit="次 / 秒" color="var(--accent-purple)" /><TrendChart samples={samples} field="recursiveHit" label="递归命中率" unit="命中率" color="var(--accent-teal)" /><TrendChart samples={samples} field="forwardHit" label="转发命中率" unit="命中率" color="var(--accent-amber)" /></div><p className="suite-note">无请求不计算比例，采集缺口不连线。</p></details>
    </Section>
    <details className="suite-home-auxiliary"><summary>辅助服务（{auxiliary.length}）</summary><div className="suite-home-components">{auxiliary.map((name) => <Component key={name} name={name} runtime={runtime} />)}</div></details>
    {gateway?.enabled && <a className="suite-gateway-link" href="#/gateway"><strong>{gateway.name ?? 'LovestGateway'}</strong><span>查看网关观测 →</span></a>}
  </div>;
}

function Component({ name, runtime }: { name: string; runtime: RuntimeSnapshot | null }) {
  const item = runtime?.components[name];
  if (!item) return <div className="suite-home-component"><strong>{name}</strong><Status value="neutral" label="未采集" /></div>;
  const summary = componentSummary(name, item);
  const layer = runtime?.layers[name === 'unbound-raw' ? 'recursive' : name === 'unbound-forward' ? 'forward' : ''];
  const listeners = runtime?.dns?.listeners[name];
  return <details className="suite-home-component"><summary><span><strong>{summary.label}</strong><small>{summary.description}</small></span><Status value={summary.tone} label={summary.status} /></summary>
    <dl className="suite-kpi-list"><div><dt>PID</dt><dd>{item.pid || '—'}</dd></div><div><dt>启动时间</dt><dd>{when(item.start)}</dd></div><div><dt>退出码</dt><dd>{item.exitstatus ?? '—'}</dd></div><div><dt>进程状态</dt><dd>{item.statename}</dd></div>{layer && <div><dt>累计查询</dt><dd>{layer.counters?.['total.num.queries']?.toLocaleString() ?? '—'}</dd></div>}{listeners && <div><dt>监听</dt><dd>{listeners.available ? listeners.endpoints.map((endpoint) => `${dnsAddress(endpoint.address, endpoint.port)} / ${endpoint.transport.toUpperCase()}`).join(' · ') || '未观察到监听 socket' : '未确认'}</dd></div>}</dl>
    {layer && !layer.available && <Empty title="通道数据不可用">{layer.error ?? '尚未取得通道统计'}</Empty>}<div className="suite-local-links"><a href={`#/system?component=${encodeURIComponent(name)}`}>最近日志</a><a href={name === 'redis' ? '#/cache' : name.startsWith('unbound') ? '#/advanced?file=unbound_custom.conf' : '#/env'}>相关配置</a></div>
  </details>;
}
