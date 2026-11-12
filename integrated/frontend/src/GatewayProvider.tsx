import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { gatewayConfig } from './gatewayApi';
import type { GatewayConfig } from './gatewayApi';
import { GatewayContext } from './gatewayContext';
import { errorText } from './suiteApi';
import { useSuite } from './suiteContext';

export default function GatewayProvider({ children }: { children: ReactNode }) {
  const { caps } = useSuite();
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
      if (!abort.signal.aborted) { setConfig(value); setError(''); }
    } catch (e) {
      // Local configuration controls navigation; a failed read must not remove a known entry.
      if (!abort.signal.aborted) setError(errorText(e));
    } finally { if (!abort.signal.aborted) setLoading(false); }
  }, []);
  useEffect(() => {
    if (!caps?.instance_id) return;
    const load = () => { void reload(); };
    const timer = window.setTimeout(load, 0);
    window.addEventListener('lovestdns-gateway-changed', load);
    window.addEventListener('focus', load);
    return () => {
      window.clearTimeout(timer); request.current?.abort();
      window.removeEventListener('lovestdns-gateway-changed', load);
      window.removeEventListener('focus', load);
    };
  }, [caps?.instance_id, reload]);
  return <GatewayContext.Provider value={{ config, loading, error, reload }}>{children}</GatewayContext.Provider>;
}
