import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import * as api from './api';
import { ApiError, errorText } from './suiteApi';
import { applyFileSave, draftEpoch, fileBaselineVersion, fileDraft, reconcileFileDraft, rememberFileDraft, subscribeFileDraft } from './fileDrafts';
import type { FileDraft } from './fileDrafts';
import { saveFeedback } from './saveFeedback';

export function useFileEditor(file: string, initial = '', enabled = true) {
  const fallback = useMemo<FileDraft>(() => ({ content: initial, baseline: null, revision: 'missing', loaded: false }), [initial]);
  const latest = useCallback(() => fileDraft(file) ?? fallback, [file, fallback]);
  const subscribe = useCallback((listener: () => void) => subscribeFileDraft(file, listener), [file]);
  const document = useSyncExternalStore(subscribe, latest);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ success: boolean; msg: string; result?: api.SaveResult } | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [epoch] = useState(draftEpoch);
  const update = useCallback((value: FileDraft) => rememberFileDraft(file, value, epoch), [file, epoch]);
  const load = useCallback(async (force = false) => {
    const saved = fileDraft(file);
    if (force && saved?.loaded && saved.content !== (saved.baseline ?? '') && !window.confirm('重新读取会替换此文件的未保存草稿。请先复制或下载需要保留的内容。确定继续？')) return;
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    const id = ++generation.current;
    setBusy(true); setError(''); setResult(null);
    try {
      const value = await api.readFile(file, abort.signal);
      if (id === generation.current) {
        const merged = reconcileFileDraft(latest(), { content: value.content, baseline: value.content, revision: value.revision!, loaded: true }, force);
        update(merged.document);
        if (merged.conflict) setError('磁盘文件已变化，当前草稿已保留。请复制或下载草稿，重新读取后合并。');
      }
    } catch (e) {
      if (id !== generation.current || abort.signal.aborted) return;
      if (e instanceof ApiError && e.status === 404) {
        const merged = reconcileFileDraft(latest(), { content: initial, baseline: null, revision: 'missing', loaded: true }, force);
        update(merged.document);
        if (merged.conflict) setError('磁盘文件已被移除，当前草稿已保留。请核对后重新读取或合并。');
      }
      else setError(errorText(e));
    } finally { if (id === generation.current) setBusy(false); }
  }, [file, initial, latest, update]);
  useEffect(() => {
    if (!enabled) return;
    const version = generation;
    const timer = setTimeout(() => void load(), 0);
    return () => { clearTimeout(timer); version.current++; controller.current?.abort(); };
  }, [load, enabled]);
  const setContent = useCallback((content: string) => {
    update({ ...latest(), content }); setResult(null); setError('');
  }, [latest, update]);
  const save = async () => {
    const baseline = latest();
    const content = baseline.content;
    if (busy || !baseline.loaded || content === baseline.baseline) return;
    const baselineVersion = fileBaselineVersion(file);
    setBusy(true); setError(''); setResult(null);
    try {
      const saved = await api.writeFile(file, content, baseline.revision);
      if (!applyFileSave(file, baselineVersion, content, saved.revision!, epoch)) return;
      const value = { success: true, msg: saveFeedback(saved), result: saved };
      setResult(value); window.dispatchEvent(new Event('lovestdns-config-changed')); return value;
    } catch (e) {
      const value = { success: false, msg: errorText(e) };
      setResult(value); return value;
    } finally { setBusy(false); }
  };
  return { content: document.content, baseline: document.baseline, loaded: document.loaded, busy, error, result,
    dirty: document.loaded && document.content !== (document.baseline ?? ''), setContent, load, save };
}
