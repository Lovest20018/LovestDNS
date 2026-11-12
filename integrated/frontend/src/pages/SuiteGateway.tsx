import { useGateway } from '../gatewayContext';
import GatewayObservation from '../components/GatewayObservation';
import { Empty, Notice } from '../components/SuiteUI';

export default function SuiteGateway() {
  const { config, loading, error, reload } = useGateway();
  return <div className="suite-page">
    <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/settings?tab=gateway">网关联动设置</a></div>
    {error && <Notice tone="error">{error} <button className="btn btn-secondary btn-sm" disabled={loading} onClick={() => void reload()}>重新读取配置</button></Notice>}
    {config?.enabled ? <GatewayObservation key={config.revision} /> : loading ? <p role="status">正在读取联动配置…</p> : <Empty title={error ? '联动状态待确认' : '尚未启用网关联动'}>在设置中连接只读 API 后，这里会展示客户端、目标与网关处理记录。</Empty>}
  </div>;
}
