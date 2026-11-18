import { createContext, useContext } from 'react';
import type { MaintenanceRequest } from './maintenanceRequests';
import type { MaintenanceTask } from './suiteApi';

export const TASK_NAMES = {
  restart_dns_core: '重启 DNS',
  apply_custom_mod: '应用自定义分流',
  save_redis_snapshot: '保存 Redis 快照',
};
export const isActiveTask = (task: MaintenanceTask) => task.state === 'queued' || task.state === 'running';
const STATES = { queued: '请求已受理 · 等待执行', running: '正在处理', succeeded: '已完成', failed: '失败', interrupted: '已中断' };
export function taskLabel(task: MaintenanceTask) {
  if (task.action === 'restart_dns_core' && task.state === 'running') return '正在重启 DNS，解析可能短暂中断';
  if (task.action !== 'save_redis_snapshot' && task.state === 'succeeded') {
    return task.action === 'restart_dns_core' ? '重启完成 · DNS 已恢复应答' : '分流已加载 · DNS 已恢复应答';
  }
  return `${TASK_NAMES[task.action]} · ${STATES[task.state]}`;
}
export type Submission = { action: MaintenanceTask['action']; phase: 'sending' | 'uncertain' | 'failed'; detail?: string };
export const MaintenanceContext = createContext<{
  tasks: MaintenanceTask[]; loaded: boolean; reading: boolean; readError: string;
  submission: Submission | null; pending: MaintenanceRequest | null; lookupMissing: boolean;
  refresh: () => Promise<void>;
  submit: (action: MaintenanceTask['action'], expected?: string | null) => Promise<boolean>;
  dismissExpired: () => void;
} | null>(null);

export function useMaintenance() {
  const value = useContext(MaintenanceContext);
  if (!value) throw new Error('MaintenanceProvider is missing');
  return value;
}

export const requestExpired = (request: MaintenanceRequest) => Date.now() - request.created >= 6 * 86400000;
