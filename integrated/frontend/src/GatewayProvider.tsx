import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { gatewayConfig } from './gatewayApi';
import type { GatewayConfig } from './gatewayApi';
import type { GatewayConnectionTest } from './gatewayContext';
import { GatewayContext } from './gatewayContext';
import { errorText } from './suiteApi';
import { useSuite } from './suiteContext';

export default function GatewayProvider({ children }: { children: ReactNode }) {
  const { caps } = useSuite();
  const setupKey = `lovestdns-gateway-setup:${caps?.instance_id ?? ''}`;
  const [setupRequested, setSetupState] = useState(() => {
    try { return sessionStorage.getItem(setupKey) === '1'; } catch { return false; }
  });
  const setSetupRequested = useCallback((requested: boolean) => {
    setSetupState(requested);
    try {
      if (requested) sessionStorage.setItem(setupKey, '1');
      else sessionStorage.removeItem(setupKey);
    } catch { /* The current page can still use the setup tab. */ }
  }, [setupKey]);
  const [connectionTest, setConnectionTest] = useState<GatewayConnectionTest | null>(null);
  const [config, setConfig] = useState<GatewayConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const reload = useCallback(async () => {
    request.current?.abort();
    const abort = new AbortController(); request.current = abort;
    setLoading(true);
    try {
      const value = await gatewayConfig(abort.signal);
      if (!abort.signal.aborted) {
        setConfig(value); setError('');
        if (value.enabled) setSetupRequested(false);
      }
    } catch (e) {
      // Local configuration controls navigation; a failed read must not remove a known entry.
      if (!abort.signal.aborted) setError(errorText(e));
    } finally { if (!abort.signal.aborted) setLoading(false); }
  }, [setSetupRequested]);
  const applyConfig = useCallback((value: GatewayConfig) => {
    request.current?.abort();
    setConfig(value);
    setError('');
    setLoading(false);
    setConnectionTest(null);
    setSetupRequested(false);
  }, [setSetupRequested]);
  useEffect(() => {
    if (!caps?.instance_id) return;
    const load = () => { void reload(); };
    const timer = window.setTimeout(load, 0);
    window.addEventListener('focus', load);
    return () => {
      window.clearTimeout(timer); request.current?.abort();
      window.removeEventListener('focus', load);
    };
  }, [caps?.instance_id, reload]);
  return <GatewayContext.Provider value={{ config, loading, error, reload, connectionTest, setConnectionTest,
    settingsVisible: config?.enabled === true || setupRequested, setSetupRequested, applyConfig }}>{children}</GatewayContext.Provider>;
}
