import { useEffect } from 'react';

const drafts = new Map<symbol, boolean>();
export function hasUnsavedChanges() { return [...drafts.values()].some(Boolean); }
export function confirmDiscard() { return !hasUnsavedChanges() || window.confirm('有未保存的配置，离开后会丢失修改。确定继续？'); }
export function useUnsavedChanges(dirty: boolean) {
  useEffect(() => {
    const id = Symbol();
    drafts.set(id, dirty);
    const unload = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', unload);
    return () => { drafts.delete(id); window.removeEventListener('beforeunload', unload); };
  }, [dirty]);
}
