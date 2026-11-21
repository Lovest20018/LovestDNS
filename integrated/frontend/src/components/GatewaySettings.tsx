import { useCallback, useEffect, useRef, useState } from 'react';
import { Cable, RefreshCw } from 'lucide-react';
import { errorText, suiteFetch } from '../suiteApi';
import { disconnectGateway, gatewayConfig } from '../gatewayApi';
import type { GatewayConfig, GatewaySummary } from '../gatewayApi';
import { Section, Notice } from './SuiteUI';
import GatewaySummaryView from './GatewaySummaryView';
import { useGateway } from '../gatewayContext';
import { useUnsavedChanges } from '../unsaved';

export function GatewayOptIn() {
  const { config, loading, error, reload, settingsVisible, setSetupRequested, setConnectionTest, applyConfig } = useGateway();
  const [disconnecting, setDisconnecting] = useState(false);
  const [disconnectError, setDisconnectError] = useState('');
  const toggle = async (checked: boolean) => {
    setDisconnectError('');
    if (checked || !config?.enabled) {
      setSetupRequested(checked);
      if (!checked) setConnectionTest(null);
      return;
    }
    if (!window.confirm('关闭网关联动会断开连接并清除已保存的只读 Token。确定关闭？')) return;
    setDisconnecting(true);
    try { applyConfig(await disconnectGateway(config.revision)); }
    catch (e) { setDisconnectError(errorText(e)); }
    finally { setDisconnecting(false); }
  };
  return <Section title="LovestGateway 联动" icon={Cable}>
    <label className="suite-gateway-toggle">
      <input type="checkbox" checked={settingsVisible} disabled={!config || disconnecting}
        aria-describedby="gateway-setup-status" onChange={(event) => void toggle(event.target.checked)} />
      启用网关联动
    </label>
    <p className="suite-note" id="gateway-setup-status" role="status">{disconnecting ? '正在断开网关连接…'
      : config?.enabled ? `已连接 ${config.name ?? 'LovestGateway'}`
        : loading && !config ? '正在读取联动配置…'
          : settingsVisible ? '在「网关联动」填写地址和只读 Token，保存后开始观测。'
            : '勾选后显示「网关联动」页签。'}</p>
    {(error || disconnectError) && <Notice tone="error">{disconnectError || error} <button className="btn btn-secondary btn-sm"
      disabled={loading || disconnecting} onClick={() => { setDisconnectError(''); void reload(); }}>重新读取配置</button></Notice>}
  </Section>;
}

export default function GatewaySettings() {
  const { connectionTest, setConnectionTest, applyConfig } = useGateway();
  const previousTest = useRef(connectionTest);
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
  const load = useCallback(async (restoreTest = false) => {
    request.current?.abort(); const abort = new AbortController(); request.current = abort;
    setBusy(true);setError('');setMessage('');setSummary(null);
    try {
      const value = await gatewayConfig(abort.signal);
      if (!abort.signal.aborted) {
        const test = restoreTest && previousTest.current?.revision === value.revision ? previousTest.current : null;
        setConfig(value);
        setUrl(test?.url ?? value.url ?? '');
        setToken('');
        if (test) { setSummary(test.summary); setMessage('连接测试通过，尚未保存。'); }
        else setConnectionTest(null);
      }
    }
    catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  }, [setConnectionTest]);
  useEffect(() => { const timer = window.setTimeout(() => void load(true), 0);return () => window.clearTimeout(timer); }, [load]);
  const action = async (kind: 'test' | 'save' | 'disconnect') => {
    if (!config || busy) return;
    request.current?.abort();const abort = new AbortController(); request.current = abort;
    setBusy(true);setError('');setMessage('');setSummary(null);
    try {
      const value = kind === 'disconnect' ? await disconnectGateway(config.revision, abort.signal)
        : await suiteFetch<GatewayConfig | GatewaySummary>(kind === 'test' ? '/api/v1/gateway/test' : '/api/v1/gateway/config', {
          signal: abort.signal, method: kind === 'test' ? 'POST' : 'PUT', timeoutMs: 25000,
          body: JSON.stringify({ url, token, expected_revision: config.revision }),
        });
      if (abort.signal.aborted) return;
      if (kind === 'test') {
        const result = value as GatewaySummary;
        setSummary(result); setConnectionTest({ revision: config.revision, url, summary: result });
        setMessage('连接测试通过，尚未保存。');
      } else {
        const next = value as GatewayConfig;
        setConfig(next); setUrl(next.url ?? ''); setToken('');
        applyConfig(next);
        if (kind === 'disconnect') window.location.hash = '#/settings?tab=external';
        else setMessage('联动已启用，按需读取。');
      }
    } catch (e) { if (!abort.signal.aborted) setError(errorText(e)); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  return <div className="suite-gateway-settings" id="gateway-integration" tabIndex={-1}><Section title="连接 LovestGateway" subtitle="只读观测" icon={Cable} actions={<button className="btn btn-secondary btn-sm" disabled={busy || dirty} onClick={() => void load()}><RefreshCw size={14} />重新读取</button>}>
    <p className="suite-note">{config ? config.enabled ? `已连接 ${config.name ?? 'LovestGateway'}` : '未启用' : '读取配置…'} · 不影响独立 DNS。</p>
    <form onSubmit={(e) => { e.preventDefault();void action('save'); }}><fieldset disabled={busy || !config} className="suite-diagnostic-fields"><div className="suite-target-row"><label>固定 API 地址<input aria-label="网关 API 地址" className="form-input" type="text" maxLength={256} placeholder="http://192.0.2.10:18788/api/v1" value={url} onChange={(e) => { setUrl(e.target.value);setConnectionTest(null);setSummary(null);setMessage(''); }} /></label><label>只读 Token<input aria-label="网关只读 Token" className="form-input" type="password" autoComplete="new-password" maxLength={256} placeholder={config?.has_token ? '留空沿用同目标凭据' : '输入 observer Token'} value={token} onChange={(e) => { setToken(e.target.value);setConnectionTest(null);setSummary(null);setMessage(''); }} /></label></div><p className="suite-note">Token 仅保存在 DNS 实例私有状态目录，不回显。切换目标必须重新提供 Token。支持固定 IP 地址，HTTPS 校验证书。</p><div className="suite-button-row"><button type="button" className="btn btn-secondary" disabled={!url} onClick={() => void action('test')}>测试连接</button><button className="btn btn-primary" disabled={!url}>保存并启用</button><button type="button" className="btn btn-secondary" disabled={!config?.enabled} onClick={() => void action('disconnect')}>断开并清除凭据</button></div></fieldset></form>
    {error && <Notice tone="error">{error}</Notice>}{message && <p className="suite-note" role="status">{message}</p>}{summary && <GatewaySummaryView value={summary} />}
    {config?.enabled && <div className="suite-local-links"><a className="btn btn-secondary btn-sm" href="#/diagnostics?tab=gateway">查看网关观测 →</a></div>}
  </Section></div>;
}
