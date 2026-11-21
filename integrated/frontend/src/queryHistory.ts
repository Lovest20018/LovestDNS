import { useCallback, useEffect, useState } from 'react';
import type { MouseEvent } from 'react';
import { useSuite } from './suiteContext';

let session: string = crypto.randomUUID?.() ?? String(Math.random());
const key = 'lovestdnsQueryHistory';
type Item<T> = { scope: string; hash: string; payload: T; focus?: string; scroll?: number };

export function clearQueryHistory() { session = String(Math.random()); }

export function useQueryHistory<T>(name: string, fallback: T) {
  const { caps } = useSuite();
  const scope = [session, caps?.instance_id, caps?.access_role, name].join(':');
  const read = useCallback((): Item<T> | null => {
    const entry = window.history.state?.[key]?.[name] as Item<T> | undefined;
    return entry?.scope === scope && entry.hash === window.location.hash ? entry : null;
  }, [name, scope]);
  const [entry, setEntry] = useState(read);
  const [restoration, setRestoration] = useState(read);
  useEffect(() => {
    const sync = () => {
      const item = read();
      setEntry(item);
      setRestoration(item);
    };
    window.addEventListener('popstate', sync);
    return () => window.removeEventListener('popstate', sync);
  }, [read]);
  const write = (payload: T, position?: { focus?: string; scroll?: number }) => {
    const item = { scope, hash: window.location.hash, payload, ...position };
    const state = window.history.state;
    window.history.replaceState({ ...state, [key]: { ...state?.[key], [name]: item } }, '');
    setEntry(item);
  };
  return { entry, restoration, dismissRestoration: () => setRestoration(null), value: entry?.payload ?? fallback, write };
}

export function navigationPosition(event: MouseEvent) {
  if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = (event.target as Element).closest<HTMLAnchorElement>('a[href^="#/"]');
  if (link && link.hash !== window.location.hash) {
    return { focus: link.dataset.queryFocus, scroll: window.scrollY };
  }
}

export function restoreQueryPosition(entry: { focus?: string; scroll?: number }, fallback?: string) {
  const target = entry.focus ? CSS.escape(entry.focus) : fallback ? CSS.escape(fallback) : '';
  const element = target && Array.from(document.querySelectorAll<HTMLElement>(`[data-query-focus="${target}"]`))
    .find(node => node.getClientRects().length);
  if (element) element.focus({ preventScroll: entry.scroll !== undefined });
  if (entry.scroll !== undefined) window.scrollTo(0, entry.scroll);
}
