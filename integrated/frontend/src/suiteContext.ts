import { createContext, useContext } from 'react';
import type { Capabilities, RuntimeSnapshot } from './suiteApi';
import type { TrendSample } from './trend';

export type ProbeSample = {
  domain: string;
  resolved: boolean;
  result: { responded?: boolean; status?: string; rcode?: string; elapsed_ms?: number; checked_at?: string };
};
export type ProbeResult = { pass: boolean; tests: Record<string, ProbeSample>; server: string; port: number };
export type ProbeObservation = { result: ProbeResult; process: string | null; expired?: boolean };

export interface SuiteState {
  caps: Capabilities | null; runtime: RuntimeSnapshot | null; samples: TrendSample[];
  runningEnv: Record<string, string> | null; statusError: string; refreshing: boolean;
  loading: boolean; error: string; runtimeError: string; paused: boolean;
  runtimeStale: boolean;
  probe: ProbeObservation | null;
  probeChecking: boolean;
  probeError: string;
  runProbe: () => Promise<void>;
  refresh: () => Promise<void>; setPaused: (paused: boolean) => void;
}
export const SuiteContext = createContext<SuiteState | null>(null);
export type SuiteConfigState = Pick<SuiteState, 'caps' | 'runningEnv' | 'statusError' | 'refresh' | 'error' | 'paused'>;
export const SuiteConfigContext = createContext<SuiteConfigState | null>(null);
export function useSuiteConfig() {
  const state = useContext(SuiteConfigContext);
  if (!state) throw new Error('SuiteProvider is missing');
  return state;
}
export function useSuite() {
  const state = useContext(SuiteContext);
  if (!state) throw new Error('SuiteProvider is missing');
  return state;
}
