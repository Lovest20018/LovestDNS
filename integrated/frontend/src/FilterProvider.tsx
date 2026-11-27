import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { filterState, readFilterTask, submitFilter } from './filterApi';
import type { FilterAction, FilterState } from './filterApi';
import { FilterContext } from './filterContext';
import type { FilterDraft, FilterTask, SubscriptionDraft } from './filterContext';
import { useUnsavedChanges } from './unsaved';
import { ApiError, errorText } from './suiteApi';
import { useSuiteConfig } from './suiteContext';

export function FilterProvider({ children }: { children: ReactNode }) {
  const { caps, paused } = useSuiteConfig();
  const allowed = !!(caps?.features.dns_filter && caps.permissions.configure);
  const [state, setState] = useState<FilterState | null>(null);
  const [draft, setDraft] = useState<FilterDraft | null>(null);
  const [subscriptionDraft, setSubscriptionDraft] = useState<SubscriptionDraft | null>(null);
  const [task, setTask] = useState<FilterTask | null>(null);
  const taskPhase = task?.phase;
  useUnsavedChanges(draft !== null || !!(subscriptionDraft?.name || subscriptionDraft?.url));
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const operation = useRef<AbortController | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
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
  const available = state?.available;
  const revision = state?.revision;
  const apply = useCallback(async (action: FilterAction) => {
    if (!allowed || !available || revision === undefined) throw new Error('当前入口未提供 DNS 过滤');
    if (taskPhase === 'uncertain') throw new Error('请先到广告过滤核对上一次任务');
    if (operation.current) throw new Error('已有过滤任务正在执行');
    const controller = new AbortController();
    operation.current = controller;
    setTask({ phase: 'pending', message: '正在提交过滤任务' });
    let id: string | undefined;
    try {
      ({ id } = await submitFilter(action, revision, controller.signal));
      setTask({ id, phase: 'pending', message: '正在处理过滤任务' });
      const result = await readFilterTask(id, controller.signal, publish);
      setTask(result.job);
      if (result.job.phase === 'succeeded') {
        if (action.action === 'configure') setDraft(null);
        if (action.action === 'preview') setSubscriptionDraft(null);
      }
      return result;
    } catch (e) {
      if (!controller.signal.aborted) {
        const rejected = !id && e instanceof ApiError && e.status >= 400 && e.status < 500;
        setTask(rejected ? { phase: 'failed', message: errorText(e) } : {
          id, phase: 'uncertain',
          message: id ? '任务记录已不可用，请核对服务端任务列表和活动版本。' :
            '提交结果待核对，请检查服务端最近任务和活动版本后再决定是否重试。',
        });
        await refresh();
      }
      throw e;
    } finally {
      if (operation.current === controller) operation.current = null;
    }
  }, [allowed, available, revision, taskPhase, publish, refresh]);
  useEffect(() => {
    const first = window.setTimeout(() => void refresh(), 0);
    const timer = allowed && !paused ? window.setInterval(() => {
      if (!document.hidden && !request.current) void refresh();
    }, 30000) : undefined;
    return () => { window.clearTimeout(first); window.clearInterval(timer); request.current?.abort(); };
  }, [allowed, paused, refresh]);
  const hasState = state !== null;
  const serviceBusy = !!state?.jobs.some(job => job.phase === 'pending');
  useEffect(() => {
    if (!allowed || !serviceBusy || taskPhase === 'pending') return;
    const timer = window.setInterval(() => {
      if (!request.current && !operation.current) void refresh();
    }, 1250);
    return () => window.clearInterval(timer);
  }, [allowed, refresh, serviceBusy, taskPhase]);
  const acknowledgeTask = useCallback(() => {
    if (taskPhase === 'uncertain' && !error && !operation.current && hasState && !serviceBusy) setTask(null);
  }, [taskPhase, error, hasState, serviceBusy]);
  const value = useMemo(() => ({ state: allowed ? state : null, error, refresh, publish, draft, setDraft,
    subscriptionDraft, setSubscriptionDraft, task, acknowledgeTask, apply }),
    [allowed, state, error, refresh, publish, draft, subscriptionDraft, task, acknowledgeTask, apply]);
  return <FilterContext.Provider value={value}>{children}</FilterContext.Provider>;
}
