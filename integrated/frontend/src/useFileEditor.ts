import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import * as api from './api';
import { ApiError, errorText } from './suiteApi';
import { applyFileSave, draftEpoch, fileBaselineVersion, fileDraft, isDirtyFileDraft, reconcileFileDraft, rememberFileDraft, subscribeFileDraft } from './fileDrafts';
import type { FileDraft } from './fileDrafts';
import { configDiff } from './configDiff';
import { parseListContent } from './domainEditor';
import { parseTtlRules } from './ttlEditor';

function savedChange(file: string, before: string, after: string) {
  const lines = configDiff(before, after);
  if (!lines) return { summary: '已保存此文件。可在配置历史中核对版本。' };
  const added = lines.filter(line => line.kind === 'add').map(line => line.text);
  const removed = lines.filter(line => line.kind === 'remove').map(line => line.text);
  const changed = [...removed, ...added].join('\n');
  const values = file === 'force_ttl_rules.txt' ? parseTtlRules(changed).entries.map(rule => rule.domain)
    : /^force_(forward|dnscrypt|recurse)_list\.txt$/.test(file) || file === 'custom_cn_mark.txt'
      ? parseListContent(changed).filter(rule => !rule.comment && ['domain', 'full'].includes(rule.prefix)).map(rule => rule.value.trim())
      : [];
  const domains = new Set(values.map(value => value.toLowerCase()));
  const candidate = domains.size === 1 ? domains.values().next().value : undefined;
  const domain = candidate && /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$/.test(candidate) ? candidate : undefined;
  return { summary: `增加 ${added.length} 行，移除 ${removed.length} 行`, domain };
}

export function useFileEditor(file: string, initial = '', enabled = true) {
  const fallback = useMemo<FileDraft>(() => ({ content: initial, baseline: null, revision: 'missing', loaded: false }), [initial]);
  const latest = useCallback(() => fileDraft(file) ?? fallback, [file, fallback]);
  const subscribe = useCallback((listener: () => void) => subscribeFileDraft(file, listener), [file]);
  const document = useSyncExternalStore(subscribe, latest);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ success: boolean; msg: string; result?: api.SaveResult; change?: ReturnType<typeof savedChange> } | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [epoch] = useState(draftEpoch);
  const update = useCallback((value: FileDraft) => rememberFileDraft(file, value, epoch), [file, epoch]);
  const load = useCallback(async (force = false) => {
    const saved = fileDraft(file);
    if (force && isDirtyFileDraft(saved) && !window.confirm('重新读取会替换此文件的未保存草稿。请先复制或下载需要保留的内容。确定继续？')) return;
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
        // The template is the editor's starting text. The revision still controls conditional creation.
        const merged = reconcileFileDraft(latest(), { content: initial, baseline: initial, revision: 'missing', loaded: true }, force);
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
    if (busy || !isDirtyFileDraft(baseline)) return;
    const baselineVersion = fileBaselineVersion(file);
    setBusy(true); setError(''); setResult(null);
    try {
      const saved = await api.writeFile(file, content, baseline.revision);
      if (!applyFileSave(file, baselineVersion, content, saved.revision!, epoch)) return;
      const before = baseline.revision === 'missing' ? '' : baseline.baseline ?? '';
      const value = { success: true, msg: '已保存', result: saved, change: savedChange(file, before, content) };
      setResult(value); window.dispatchEvent(new Event('lovestdns-config-changed')); return value;
    } catch (e) {
      const value = { success: false, msg: errorText(e) };
      setResult(value); return value;
    } finally { setBusy(false); }
  };
  return { content: document.content, baseline: document.baseline, loaded: document.loaded, busy, error, result,
    dirty: isDirtyFileDraft(document), setContent, load, save };
}
