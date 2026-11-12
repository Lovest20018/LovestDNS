import { useCallback, useEffect, useRef, useState } from 'react';
import { Cable, RefreshCw } from 'lucide-react';
import { errorText, suiteFetch } from '../suiteApi';
import { gatewayConfig, gatewayChanged } from '../gatewayApi';
import type { GatewayConfig, GatewaySummary } from '../gatewayApi';
import { Section, Notice } from './SuiteUI';
import GatewaySummaryView from './GatewaySummaryView';
import { useUnsavedChanges } from '../unsaved';

export default function GatewaySettings() {
  const [config, setConfig] = useState<GatewayConfig | null>(null);
  const [url, setUrl] = useState(''); const [token, setToken] = useState('');
  const [summary, setSummary] = useState<GatewaySummary | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('');
  const request = useRef<AbortController | null>(null);
  const dirty = !!config && (url !== (config.url ?? '') || !!token);
  useUnsavedChanges(dirty);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: MouseEvent) => {
      const link = (event.target as Element).closest('a[href]');
      if (link && link.getAttribute('href') !== window.location.hash && !window.confirm('网关联动有未保存输入，离开将清除 Token 草稿。确定继续？')) { event.preventDefault();event.stopPropagation(); }
    };
    document.addEventListener('click', guard, true);return () => document.removeEventListener('click', guard, true);
  }, [dirty]);
  useEffect(() => () => request.current?.abort(), []);
  const load = useCallback(async () => {
    request.current?.abort(); const abort = new AbortController(); request.current = abort;
    setBusy(true);setError('');setMessage('');setSummary(null);
    try { const value = await gatewayConfig(abort.signal); if (!abort.signal.aborted) { setConfig(value);setUrl(value.url ?? '');setToken(''); } }
    catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  }, []);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0);return () => window.clearTimeout(timer); }, [load]);
  const action = async (kind: 'test' | 'save' | 'disconnect') => {
    if (!config || busy) return;
    request.current?.abort();const abort = new AbortController(); request.current = abort;
    setBusy(true);setError('');setMessage('');setSummary(null);
    try {
      const body = kind === 'disconnect' ? { expected_revision: config.revision } : { url, token, expected_revision: config.revision };
      const value = await suiteFetch<GatewayConfig | GatewaySummary>(kind === 'test' ? '/api/v1/gateway/test' : '/api/v1/gateway/config', { signal: abort.signal, method: kind === 'test' ? 'POST' : kind === 'save' ? 'PUT' : 'DELETE', body: JSON.stringify(body), timeoutMs: 25000 });
      if (abort.signal.aborted) return;
      if (kind === 'test') { setSummary(value as GatewaySummary);setMessage('连接测试通过，尚未保存。'); }
      else { const next = value as GatewayConfig;setConfig(next);setUrl(next.url ?? '');setToken('');gatewayChanged();setMessage(kind === 'save' ? '联动已启用，按需读取。' : '已断开并清除只读凭据。'); }
    } catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  return <div id="gateway-integration"><Section title="网关联动" subtitle="可选只读观测 · 默认关闭" icon={Cable} actions={<button className="btn btn-secondary btn-sm" disabled={busy || dirty} onClick={() => void load()}><RefreshCw size={14} />重新读取</button>}>
    <p className="suite-muted">{config ? config.enabled ? `已连接 ${config.name ?? 'LovestGateway'}` : '未启用' : '读取配置…'} · 不影响独立 DNS。</p>
    <form onSubmit={(e) => { e.preventDefault();void action('save'); }}><fieldset disabled={busy || !config} className="suite-diagnostic-fields"><div className="suite-target-row"><label>固定 API 地址<input aria-label="网关 API 地址" className="form-input" type="text" maxLength={256} placeholder="http://192.0.2.10:18788/api/v1" value={url} onChange={(e) => { setUrl(e.target.value);setSummary(null);setMessage(''); }} /></label><label>只读 Token<input aria-label="网关只读 Token" className="form-input" type="password" autoComplete="new-password" maxLength={256} placeholder={config?.has_token ? '留空沿用同目标凭据' : '输入 observer Token'} value={token} onChange={(e) => { setToken(e.target.value);setSummary(null);setMessage(''); }} /></label></div><p className="suite-note">Token 仅保存在 DNS 实例私有状态目录，不回显。切换目标必须重新提供 Token。支持固定 IP 地址，HTTPS 校验证书。</p><div className="suite-button-row"><button type="button" className="btn btn-secondary" disabled={!url} onClick={() => void action('test')}>测试连接</button><button className="btn btn-primary" disabled={!url}>保存并启用</button><button type="button" className="btn btn-secondary" disabled={!config?.enabled} onClick={() => void action('disconnect')}>断开并清除凭据</button></div></fieldset></form>
    {error && <Notice tone="error">{error}</Notice>}{message && <p role="status">{message}</p>}{summary && <GatewaySummaryView value={summary} />}
    {config?.enabled && <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/gateway">查看网关观测 →</a></div>}
  </Section></div>;
}
