import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { SuiteContext, SuiteConfigContext } from './suiteContext';
import type { ProbeObservation, ProbeResult } from './suiteContext';
import { setDraftInstance } from './fileDrafts';
import { suiteFetch, errorText } from './suiteApi';
import type { Capabilities, RuntimeSnapshot } from './suiteApi';
import { appendTrend } from './trend';
import type { TrendSample } from './trend';
import { useStore } from './store';
import type { SystemStatus } from './api';
import { runtimeLifetimeMs } from './runtimeFreshness';
import { processFingerprint } from './queryEvidence';

const PROBE_FRESH_MS = 5 * 60_000;

export default function SuiteProvider({ children }: { children: ReactNode }) {
  const [runningEnv, setRunningEnv] = useState<Record<string, string> | null>(null);
  const [statusError, setStatusError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [runtime, setRuntime] = useState<RuntimeSnapshot | null>(null);
  const [runtimeDeadline, setRuntimeDeadline] = useState(0);
  const [samples, setSamples] = useState<TrendSample[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [runtimeError, setRuntimeError] = useState('');
  const [paused, setPaused] = useState(false);
  const [runtimeStale, setRuntimeStale] = useState(false);
  const [probe, setProbe] = useState<ProbeObservation | null>(null);
  const [probeChecking, setProbeChecking] = useState(false);
  const [probeError, setProbeError] = useState('');
  const probeInput = useRef({ caps, error, runtime });
  const probeRequest = useRef<AbortController | null>(null);
  const capabilitiesCache = useRef<Capabilities | null>(null);
  const capabilitiesAt = useRef(0);
  const statusAt = useRef(0);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const refreshQueued = useRef<(() => void) | null>(null);
  const request = useRef<AbortController | null>(null);
  const sampleClock = useRef<{ serverAt: number; receivedAt: number } | null>(null);

  const appendGap = useCallback(() => {
    const clock = sampleClock.current;
    if (clock) setSamples((old) => appendTrend(old, null, clock.serverAt + (performance.now() - clock.receivedAt) / 1000));
  }, []);

  useEffect(() => () => {
    generation.current++;
    request.current?.abort();
    request.current = null;
    inFlight.current = false;
    refreshQueued.current = null;
    probeRequest.current?.abort();
    probeRequest.current = null;
  }, []);

  useEffect(() => {
    if (!probe || probe.expired) return;
    const timer = window.setTimeout(() => {
      setProbe(current => current === probe ? { ...probe, expired: true } : current);
    }, PROBE_FRESH_MS);
    return () => window.clearTimeout(timer);
  }, [probe]);

  useLayoutEffect(() => {
    probeInput.current = { caps, error, runtime };
  }, [caps, error, runtime]);

  const runProbe = useCallback(async () => {
    const { caps, error, runtime } = probeInput.current;
    if (!caps?.permissions.diagnose || error || probeRequest.current) return;
    const abort = new AbortController();
    const process = processFingerprint(runtime);
    probeRequest.current = abort;
    setProbeChecking(true);
    setProbeError('');
    try {
      const params = new URLSearchParams({ server: caps.dns_defaults.server, port: String(caps.dns_defaults.port) });
      const result = await suiteFetch<ProbeResult>(`/api/v1/health-check?${params}`, { signal: abort.signal });
      const samples = Object.values(result.tests ?? {});
      if (typeof result.pass !== 'boolean' || typeof result.server !== 'string' || !Number.isInteger(result.port)
        || samples.length === 0 || samples.some(sample => typeof sample?.domain !== 'string'
          || typeof sample.resolved !== 'boolean' || !sample.result || typeof sample.result !== 'object')) {
        throw new Error('服务没有返回有效的域名测试结果');
      }
      if (!abort.signal.aborted) setProbe({ result, process });
    } catch (e) {
      if (!abort.signal.aborted) setProbeError(errorText(e));
    } finally {
      if (probeRequest.current === abort) {
        probeRequest.current = null;
        setProbeChecking(false);
      }
    }
  }, []);

  const read = useCallback(async (force = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    const abort = new AbortController();
    request.current = abort;
    // Each resource has its own deadline. A slow capabilities request must not
    // hand an already-expired signal to otherwise healthy runtime/status reads.
    const options = { signal: abort.signal, timeoutMs: 15000 };
    const current = generation.current;
    try {
      let capabilities = capabilitiesCache.current;
      if (force || !capabilities || performance.now() - capabilitiesAt.current > 30000) {
        try {
          capabilities = await suiteFetch<Capabilities>('/api/v1/capabilities', options);
          if (current !== generation.current) return;
          if (capabilitiesCache.current && capabilitiesCache.current.instance_id !== capabilities.instance_id) {
            setRuntime(null); setSamples([]); setRunningEnv(null); statusAt.current = 0; useStore.getState().resetAll();
          }
          if (capabilitiesCache.current && (
            capabilitiesCache.current.instance_id !== capabilities.instance_id
            || capabilitiesCache.current.access_role !== capabilities.access_role
            || (capabilitiesCache.current.permissions.diagnose && !capabilities.permissions.diagnose)
          )) {
            probeRequest.current?.abort();
            probeRequest.current = null;
            setProbe(null);
            setProbeChecking(false);
            setProbeError('');
          }
          if (!capabilities.permissions.configure) { setRunningEnv(null); useStore.getState().setEnvValues({}); useStore.getState().setEnvLoaded(false); useStore.getState().setFilesExist({}); }
          setDraftInstance(capabilities.instance_id);
          capabilitiesCache.current = capabilities; capabilitiesAt.current = performance.now();
          setCaps(capabilities); setError(''); useStore.getState().setConnected(true);
        } catch (e) { if (current === generation.current) { setError(errorText(e)); useStore.getState().setConnected(false); } }
      }
      const statusRead = async () => {
        if (!capabilities?.permissions.configure || (!force && statusAt.current && performance.now() - statusAt.current < 30000)) return;
        try {
          const status = await suiteFetch<SystemStatus>('/api/v1/status', options);
          if (current !== generation.current) return;
          statusAt.current = performance.now(); setRunningEnv(status.env); setStatusError('');
          useStore.getState().setEnvValues(status.env); useStore.getState().setFilesExist(status.files);
        } catch (e) {
          if (current === generation.current) {
            setRunningEnv(null); setStatusError(errorText(e));
            useStore.getState().setEnvValues({}); useStore.getState().setEnvLoaded(false); useStore.getState().setFilesExist({});
          }
        }
      };
      const runtimeRead = async () => { try {
        const started = performance.now();
        const sample = await suiteFetch<RuntimeSnapshot>('/api/v1/runtime', options);
        if (current !== generation.current) return;
        const receivedAt = performance.now();
        const lifetime = runtimeLifetimeMs(sample, receivedAt - started);
        sampleClock.current = { serverAt: sample.sampled_at + (sample.freshness?.age_seconds ?? 0), receivedAt };
        setRuntimeDeadline(receivedAt + lifetime);
        setRuntime(sample); setRuntimeError(''); setRuntimeStale(false);
        setSamples((old) => appendTrend(old, sample, sample.sampled_at));
      } catch (e) {
        if (current !== generation.current) return;
        setRuntime(null); setRuntimeError(errorText(e));
        appendGap();
      } };
      await Promise.allSettled([statusRead(), runtimeRead()]);
    } catch (e) {
      if (current !== generation.current) return;
      setRuntime(null); setRunningEnv(null); setStatusError(errorText(e)); setError(errorText(e));
      appendGap();
      useStore.getState().setConnected(false);
    } finally {
      if (request.current === abort) { inFlight.current = false; request.current = null; }
      if (current === generation.current) {
        setLoading(false);
        setRefreshing(false);
        const next = refreshQueued.current;
        refreshQueued.current = null;
        next?.();
      }
    }
  }, [appendGap]);
  const refresh = useCallback(async () => {
    if (inFlight.current) { refreshQueued.current = () => void read(true); return; }
    await read(true);
  }, [read]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (!document.hidden && !paused) await read();
      if (!stopped && !paused) timer = setTimeout(poll, document.hidden ? 30000 : 5000);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); };
  }, [read, paused]);

  useEffect(() => {
    const invalidate = () => { capabilitiesAt.current = 0; statusAt.current = 0; void refresh(); };
    const visible = () => { if (!document.hidden && !paused) invalidate(); };
    window.addEventListener('lovestdns-config-changed', invalidate);
    document.addEventListener('visibilitychange', visible);
    return () => { window.removeEventListener('lovestdns-config-changed', invalidate); document.removeEventListener('visibilitychange', visible); };
  }, [refresh, paused]);

  useEffect(() => {
    if (!runtime) return;
    const expires = Math.max(0, runtimeDeadline - performance.now());
    const timer = setTimeout(() => {
      setRuntimeStale(true);
      if (!paused) { setRuntime(null); setRuntimeError('运行样本已过期，请刷新采样'); }
      appendGap();
    }, expires);
    return () => clearTimeout(timer);
  }, [runtime, runtimeDeadline, appendGap, paused]);

  const config = useMemo(() => ({ caps, runningEnv, statusError, refresh, error, paused }),
    [caps, runningEnv, statusError, refresh, error, paused]);
  const suite = useMemo(() => ({ runningEnv, statusError, refreshing, caps, runtime, samples, loading, error,
    runtimeError, runtimeStale, paused, probe, probeChecking, probeError, runProbe, refresh, setPaused }),
    [runningEnv, statusError, refreshing, caps, runtime, samples, loading, error,
      runtimeError, runtimeStale, paused, probe, probeChecking, probeError, runProbe, refresh]);
  return <SuiteConfigContext.Provider value={config}>
    <SuiteContext.Provider value={suite}>
      {children}
    </SuiteContext.Provider>
  </SuiteConfigContext.Provider>;
}
