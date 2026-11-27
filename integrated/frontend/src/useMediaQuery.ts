import { useMemo, useSyncExternalStore } from 'react';

export function useMediaQuery(query: string): boolean {
  const { subscribe, getSnapshot } = useMemo(() => {
    const media = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query) : null;
    return {
      subscribe(listener: () => void) {
        if (!media) return () => {};
        media.addEventListener('change', listener);
        return () => media.removeEventListener('change', listener);
      },
      getSnapshot: () => media?.matches ?? false,
    };
  }, [query]);
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
