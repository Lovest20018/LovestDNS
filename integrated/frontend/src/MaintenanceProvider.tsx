import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, errorText, suiteFetch } from './suiteApi';
import type { MaintenanceTask } from './suiteApi';
import { clearMaintenanceRequests, createMaintenanceRequest, pendingMaintenanceRequest, rememberMaintenanceRequest } from './maintenanceRequests';
import { isActiveTask, MaintenanceContext, requestExpired } from './maintenanceContext';
import type { Submission } from './maintenanceContext';

export default function MaintenanceProvider({ instance, allowed, children }: { instance?: string; allowed: boolean; children: ReactNode }) {
  const [tasks, setTasks] = useState<MaintenanceTask[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState('');
  const [lookupMissing, setLookupMissing] = useState(false);
  const [submission, setSubmission] = useState<Submission | null>(() => {
    const pending = pendingMaintenanceRequest(instance);
    return pending && { action: pending.action, phase: 'uncertain' };
  });
  const latest = useRef<MaintenanceTask[]>([]);
  const inFlight = useRef<AbortController | null>(null);
  const sending = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const receive = useCallback((items: MaintenanceTask[], accepted?: MaintenanceTask) => {
    const before = latest.current;
    const completed = items.some(task => !isActiveTask(task) && before.some(old => old.id === task.id && isActiveTask(old)));
    latest.current = items;
    setTasks(items);
    if (completed || (accepted && !isActiveTask(accepted))) {
      window.dispatchEvent(new Event('lovestdns-config-changed'));
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!allowed || inFlight.current || sending.current) return;
    const abort = new AbortController();
    inFlight.current = abort;
    setReading(true);
    const start = generation.current;
    try {
      const { tasks: rows } = await suiteFetch<{ tasks: MaintenanceTask[] }>('/api/maintenance/tasks', { signal: abort.signal, timeoutMs: 5000 });
      const pending = sending.current ? null : pendingMaintenanceRequest(instance);
      let recovered: MaintenanceTask | undefined;
      if (pending) {
        try {
          recovered = (await suiteFetch<{ task: MaintenanceTask }>(`/api/maintenance/tasks/${pending.request_id}`, { signal: abort.signal, timeoutMs: 5000 })).task;
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 404) throw error;
        }
      }
      if (abort.signal.aborted || start !== generation.current) return;
      if (recovered) {
        clearMaintenanceRequests();
        setSubmission(null);
      } else if (pending?.retryRejected) {
        // The retry was rejected and a fresh lookup also found no accepted task.
        clearMaintenanceRequests();
        setSubmission(current => ({
          action: pending.action, phase: 'failed',
          detail: current?.detail ?? '原编号重试被拒绝，未查到受理记录。',
        }));
      }
      setLookupMissing(Boolean(pending && !recovered && !pending.retryRejected));
      receive(recovered ? [recovered, ...rows.filter(row => row.id !== recovered.id)] : rows, recovered);
      setLoaded(true);
      setReadError('');
    } catch (error) {
      if (!abort.signal.aborted && start === generation.current) {
        setReadError(errorText(error));
        setLookupMissing(false);
      }
    } finally {
      if (inFlight.current === abort) {
        inFlight.current = null;
        setReading(false);
      }
    }
  }, [allowed, instance, receive]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden) await refresh();
      if (!stopped) timer = setTimeout(poll, pendingMaintenanceRequest(instance) || latest.current.some(isActiveTask) ? 1800 : 7000);
    };
    const visible = () => { if (!document.hidden) void refresh(); };
    if (allowed) {
      void poll();
      window.addEventListener('online', visible);
      document.addEventListener('visibilitychange', visible);
    }
    return () => {
      stopped = true;
      clearTimeout(timer);
      inFlight.current?.abort();
      inFlight.current = null;
      sending.current?.abort();
      window.removeEventListener('online', visible);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [allowed, instance, refresh]);

  const submit = async (action: MaintenanceTask['action'], expected: string | null = null) => {
    if (!instance || !allowed || sending.current || latest.current.some(isActiveTask)) return false;
    const prior = pendingMaintenanceRequest(instance);
    if (prior && (!lookupMissing || prior.retryRejected || requestExpired(prior) || prior.action !== action || prior.expected_sha256 !== expected)) return false;
    let request;
    try {
      request = prior ?? createMaintenanceRequest(action, expected);
    } catch (error) {
      setSubmission({ action, phase: 'failed', detail: `请求未发出。 ${errorText(error)}` });
      return false;
    }
    const body = { action, request_id: request.request_id, expected_sha256: expected };
    const abort = new AbortController();
    sending.current = abort;
    generation.current++;
    inFlight.current?.abort();
    inFlight.current = null;
    setReading(false);
    rememberMaintenanceRequest(instance, request);
    setLookupMissing(false);
    setSubmission({ action, phase: 'sending' });
    try {
      const { task } = await suiteFetch<{ task: MaintenanceTask }>('/api/maintenance/tasks', {
        method: 'POST', body: JSON.stringify(body), signal: abort.signal, timeoutMs: 6000,
      });
      if (abort.signal.aborted || pendingMaintenanceRequest(instance)?.request_id !== request.request_id) return false;
      clearMaintenanceRequests();
      setReadError('');
      setSubmission(null);
      receive([task, ...latest.current.filter(row => row.id !== task.id)].slice(0, 20), task);
      return true;
    } catch (error) {
      if (abort.signal.aborted || pendingMaintenanceRequest(instance)?.request_id !== request.request_id) return false;
      const rejected = error instanceof ApiError && [400, 403, 409, 412, 415].includes(error.status);
      if (rejected) {
        if (prior) rememberMaintenanceRequest(instance, { ...request, retryRejected: true });
        else clearMaintenanceRequests();
      }
      setSubmission({ action, phase: rejected && !prior ? 'failed' : 'uncertain', detail: errorText(error) });
      return false;
    } finally {
      sending.current = null;
      generation.current++;
      if (!abort.signal.aborted) void refresh();
    }
  };

  const pending = pendingMaintenanceRequest(instance);
  const dismissExpired = () => {
    if (!pending || !lookupMissing || !requestExpired(pending)) return;
    clearMaintenanceRequests();
    setLookupMissing(false);
    setSubmission(null);
  };
  return <MaintenanceContext.Provider value={{ tasks, loaded, reading, readError, pending, lookupMissing, submission, refresh, submit, dismissExpired }}>
    {children}
  </MaintenanceContext.Provider>;
}
