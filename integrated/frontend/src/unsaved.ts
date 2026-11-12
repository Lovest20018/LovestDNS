import { useEffect } from 'react';
import { dirtyFileDrafts } from './fileDrafts';

const drafts = new Map<symbol, boolean>();
export function hasUnsavedChanges() { return dirtyFileDrafts() || [...drafts.values()].some(Boolean); }
export function confirmDiscard() { return !hasUnsavedChanges() || window.confirm('有未保存的配置，退出会清除内存中的草稿。确定继续？'); }
export function useUnsavedChanges(dirty: boolean) {
  useEffect(() => {
    const id = Symbol();
    drafts.set(id, dirty);
    const unload = (event: BeforeUnloadEvent) => { if (hasUnsavedChanges()) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', unload);
    return () => { drafts.delete(id); window.removeEventListener('beforeunload', unload); };
  }, [dirty]);
}
