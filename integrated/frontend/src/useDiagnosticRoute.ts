import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { diagnosticHash, parseDiagnosticRoute } from './diagnosticRoute';
import type { DiagnosticRoute } from './diagnosticRoute';

const STORAGE_PREFIX = 'lovestdns-diagnostics:';
const subscribe = (listener: () => void) => {
  window.addEventListener('hashchange', listener);
  return () => window.removeEventListener('hashchange', listener);
};
const snapshot = () => window.location.hash;

export function clearDiagnosticRoutes() {
  try {
    for (const key of Object.keys(sessionStorage)) if (key.startsWith(STORAGE_PREFIX)) sessionStorage.removeItem(key);
  } catch { /* Navigation also works when browser storage is unavailable. */ }
}

export function useDiagnosticRoute(instance: string, canRecord: boolean) {
  const hash = useSyncExternalStore(subscribe, snapshot);
  const key = `${STORAGE_PREFIX}${instance}:${canRecord ? 'admin' : 'read'}`;
  let remembered = '';
  try { remembered = sessionStorage.getItem(key) ?? ''; } catch { /* Use the URL alone. */ }
  const route = parseDiagnosticRoute(hash, remembered, canRecord);
  const canonical = diagnosticHash(route);
  useEffect(() => {
    if (!instance || hash.split('?')[0] !== '#/diagnostics') return;
    try { sessionStorage.setItem(key, canonical); } catch { /* URL remains shareable. */ }
    if (window.location.hash === hash && hash !== canonical) {
      window.history.replaceState(null, '', canonical);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    }
  }, [canonical, hash, instance, key]);
  const navigate = useCallback((patch: Partial<DiagnosticRoute>, replace = false) => {
    const next = diagnosticHash({ ...parseDiagnosticRoute(window.location.hash, '', canRecord), ...patch });
    if (replace) {
      window.history.replaceState(null, '', next);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    } else if (window.location.hash !== next) window.location.hash = next;
  }, [canRecord]);
  return { route, navigate };
}
