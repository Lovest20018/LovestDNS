import { createContext, useContext } from 'react';
import type { Capabilities, RuntimeSnapshot } from './suiteApi';
import type { TrendSample } from './trend';

export interface SuiteState {
  caps: Capabilities | null; runtime: RuntimeSnapshot | null; samples: TrendSample[];
  runningEnv: Record<string, string> | null; statusError: string; refreshing: boolean;
  loading: boolean; error: string; runtimeError: string; paused: boolean;
  refresh: () => Promise<void>; setPaused: (paused: boolean) => void;
}
export const SuiteContext = createContext<SuiteState | null>(null);
export function useSuite() {
  const state = useContext(SuiteContext);
  if (!state) throw new Error('SuiteProvider is missing');
  return state;
}
