import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { filterState } from './filterApi';
import type { FilterState } from './filterApi';
import { FilterContext } from './filterContext';
import type { FilterDraft } from './filterContext';
import { useUnsavedChanges } from './unsaved';
import { errorText } from './suiteApi';
import { useSuite } from './suiteContext';

export function FilterProvider({ children }: { children: ReactNode }) {
  const { caps, paused } = useSuite();
  const allowed = !!(caps?.features.dns_filter && caps.permissions.configure);
  const [state, setState] = useState<FilterState | null>(null);
  const [draft, setDraft] = useState<FilterDraft | null>(null);
  useUnsavedChanges(draft !== null);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const publish = useCallback((value: FilterState) => {
    request.current?.abort();
    setState(value);
    setError('');
  }, []);
  const refresh = useCallback(async () => {
    if (!allowed) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const value = await filterState(controller.signal);
      if (!controller.signal.aborted) { setState(value); setError(''); }
    } catch (e) {
      if (!controller.signal.aborted) setError(errorText(e));
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, [allowed]);
  useEffect(() => {
    const first = window.setTimeout(() => void refresh(), 0);
    const timer = allowed && !paused ? window.setInterval(() => {
      if (!document.hidden && !request.current) void refresh();
    }, 30000) : undefined;
    return () => { window.clearTimeout(first); window.clearInterval(timer); request.current?.abort(); };
  }, [allowed, paused, refresh]);
  return <FilterContext.Provider value={{ state: allowed ? state : null, error, refresh, publish, draft, setDraft }}>{children}</FilterContext.Provider>;
}
