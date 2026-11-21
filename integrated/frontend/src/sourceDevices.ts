import { useEffect, useSyncExternalStore } from 'react';
import { errorText, suiteFetch } from './suiteApi';
import { useSuite } from './suiteContext';

type Devices = Record<string, string>;
type Snapshot = { devices: Devices; error: string; loaded: boolean };
type Entry = { snapshot: Snapshot; pending?: Promise<void>; writes: Promise<unknown>; generation: number };
const empty: Snapshot = { devices: {}, error: '', loaded: false };
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach(listener => listener());
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
export function clearSourceDevices() { entries.clear(); emit(); }

async function request(instance: string, init?: RequestInit) {
  const result = await suiteFetch<{ instance_id: string; devices: Devices }>('/api/v1/source-devices', init);
  if (result.instance_id !== instance || !result.devices || typeof result.devices !== 'object'
    || Array.isArray(result.devices) || !Object.values(result.devices).every(value => typeof value === 'string')) {
    throw new Error('来源设备响应无效或实例已变化');
  }
  return result.devices;
}

function entryFor(key: string): Entry {
  const current = entries.get(key);
  if (current) return current;
  const entry = { snapshot: empty, writes: Promise.resolve(), generation: 0 };
  entries.set(key, entry);
  return entry;
}

function load(key: string, instance: string) {
  const entry = entryFor(key);
  if (entry.pending) return;
  const generation = entry.generation;
  const publish = (snapshot: Snapshot) => {
    if (entries.get(key) !== entry || entry.generation !== generation) return;
    entry.snapshot = snapshot;
    emit();
  };
  entry.pending = request(instance).then(devices => publish({ devices, error: '', loaded: true }))
    .catch(error => publish({ ...entry.snapshot, error: errorText(error) }))
    .finally(() => { entry.pending = undefined; });
}

export function useSourceDevices() {
  const { caps } = useSuite();
  const instance = caps?.instance_id ?? '';
  const key = instance && caps?.permissions.configure ? instance + ':' + caps.access_role : '';
  const snapshot = useSyncExternalStore(subscribe, () => entries.get(key)?.snapshot ?? empty);
  useEffect(() => {
    if (key && !entries.get(key)?.snapshot.loaded) load(key, instance);
  }, [key, instance]);
  return {
    ...snapshot,
    allowed: !!key,
    reload: () => { if (key) load(key, instance); },
    save: async (ip: string, name: string) => {
      if (!key) throw new Error('需要管理权限');
      const entry = entryFor(key);
      const write = entry.writes.then(async () => {
        if (entries.get(key) !== entry) throw new Error('实例会话已结束');
        ++entry.generation;
        const devices = await request(instance, { method: 'PUT', body: JSON.stringify({ ip, name }) });
        if (entries.get(key) === entry) {
          ++entry.generation;
          entry.snapshot = { devices, error: '', loaded: true };
          emit();
        }
      });
      entry.writes = write.catch(() => undefined);
      await write;
    },
  };
}
