import { createContext, useContext } from 'react';
import type { UpdateNetwork } from './suiteApi';

export type UpdateNetworkDraft = {
  mode: UpdateNetwork['mode']; channel: UpdateNetwork['channel']; host: string;
  port: string; username: string; password: string; clear_password: boolean;
};

export const UpdateNetworkContext = createContext<{
  saved: UpdateNetwork | null;
  draft: UpdateNetworkDraft | null;
  dirty: boolean;
  saving: boolean;
  message: string;
  error: string;
  load: (signal?: AbortSignal) => Promise<void>;
  edit: (change: Partial<UpdateNetworkDraft>) => void;
  restore: () => void;
  save: () => Promise<void>;
} | null>(null);

export function useUpdateNetwork() {
  const value = useContext(UpdateNetworkContext);
  if (!value) throw new Error('UpdateNetworkProvider is missing');
  return value;
}
