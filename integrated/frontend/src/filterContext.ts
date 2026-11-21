import { createContext, useContext } from 'react';
import type { FilterAction, FilterSourceType, FilterState, readFilterTask } from './filterApi';

export interface FilterDraft { revision: string; enabled: boolean; local: string }
export interface SubscriptionDraft { name: string; url: string; interval: number; type: FilterSourceType }
export interface FilterTask {
  phase: 'pending' | 'uncertain' | 'succeeded' | 'failed';
  id?: string;
  message: string;
}
interface FilterView {
  draft: FilterDraft | null;
  setDraft: (draft: FilterDraft | null) => void;
  subscriptionDraft: SubscriptionDraft | null;
  setSubscriptionDraft: (draft: SubscriptionDraft | null) => void;
  task: FilterTask | null;
  acknowledgeTask: () => void;
  apply: (action: FilterAction) => ReturnType<typeof readFilterTask>;
  state: FilterState | null;
  error: string;
  refresh: () => Promise<void>;
  publish: (state: FilterState) => void;
}
export const FilterContext = createContext<FilterView | null>(null);
export function useFilterView() {
  const view = useContext(FilterContext);
  if (!view) throw new Error('FilterProvider is missing');
  return view;
}

export function filterStatus(state: FilterState | null) {
  if (!state) return '过滤状态未确认';
  if (state.mode === 'recursive') return '纯递归 · 过滤不可用';
  if (!state.available) return '过滤入口不可用';
  if (state.revision !== state.live_revision) return '过滤版本待核对';
  return state.live_enabled ? '过滤生效' : '过滤未启用';
}
