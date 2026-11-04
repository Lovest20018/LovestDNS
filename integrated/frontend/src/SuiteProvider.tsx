import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { SuiteContext } from './suiteContext';
import { suiteFetch, errorText } from './suiteApi';
import type { Capabilities, RuntimeSnapshot } from './suiteApi';
import { appendTrend } from './trend';
import type { TrendSample } from './trend';
import { useStore } from './store';
import type { SystemStatus } from './api';

export default function SuiteProvider({ children }: { children: ReactNode }) {
  const [runningEnv, setRunningEnv] = useState<Record<string, string> | null>(null);
  const [statusError, setStatusError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [runtime, setRuntime] = useState<RuntimeSnapshot | null>(null);
  const [samples, setSamples] = useState<TrendSample[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [runtimeError, setRuntimeError] = useState('');
  const [paused, setPaused] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => { generation.current++; request.current?.abort(); request.current = null; inFlight.current = false; }, []);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    const abort = new AbortController();
    request.current = abort;
    const options = { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(15000)]) };
    const current = generation.current;
    try {
      const capabilities = await suiteFetch<Capabilities>('/api/v1/capabilities', options);
      if (current !== generation.current) return;
      setCaps(capabilities); setError('');
      useStore.getState().setConnected(true);
      if (capabilities.permissions.configure) {
        try {
          const status = await suiteFetch<SystemStatus>('/api/v1/status', options);
          if (current !== generation.current) return;
          setRunningEnv(status.env); setStatusError('');
          useStore.getState().setEnvValues(status.env); useStore.getState().setFilesExist(status.files);
        } catch (e) { if (current === generation.current) { setRunningEnv(null); setStatusError(errorText(e)); } }
      }
      try {
        const sample = await suiteFetch<RuntimeSnapshot>('/api/v1/runtime', options);
        if (current !== generation.current) return;
        const age = Date.now() / 1000 - sample.sampled_at;
        if (!Number.isFinite(age) || age < 0 || age > 25) throw new Error('运行样本已过期或时钟不一致');
        setRuntime(sample); setRuntimeError('');
        setSamples((old) => appendTrend(old, sample, Date.now() / 1000));
      } catch (e) {
        if (current !== generation.current) return;
        setRuntime(null); setRuntimeError(errorText(e));
        setSamples((old) => appendTrend(old, null, Date.now() / 1000));
      }
    } catch (e) {
      if (current !== generation.current) return;
      setRuntime(null); setRunningEnv(null); setStatusError(errorText(e)); setError(errorText(e));
      setSamples((old) => appendTrend(old, null, Date.now() / 1000));
      useStore.getState().setConnected(false);
    } finally {
      if (request.current === abort) { inFlight.current = false; request.current = null; }
      if (current === generation.current) { setLoading(false); setRefreshing(false); }
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!stopped && !paused) timer = setTimeout(poll, 5000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [refresh, paused]);

  useEffect(() => {
    if (!runtime) return;
    const expires = Math.max(0, (runtime.sampled_at + 25) * 1000 - Date.now());
    const timer = setTimeout(() => {
      setRuntime(null); setRuntimeError('运行样本已超过 25 秒，请刷新采样');
      setSamples((old) => appendTrend(old, null, Date.now() / 1000));
    }, expires);
    return () => clearTimeout(timer);
  }, [runtime]);

  return <SuiteContext.Provider value={{ runningEnv, statusError, refreshing, caps, runtime, samples, loading, error, runtimeError, paused, refresh, setPaused }}>{children}</SuiteContext.Provider>;
}
