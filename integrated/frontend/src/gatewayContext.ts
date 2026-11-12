import { createContext, useContext } from 'react';
import type { GatewayConfig } from './gatewayApi';

export interface GatewayState {
  config: GatewayConfig | null;
  loading: boolean;
  error: string;
  reload: () => Promise<void>;
}
export const GatewayContext = createContext<GatewayState | null>(null);
export function useGateway() {
  const state = useContext(GatewayContext);
  if (!state) throw new Error('GatewayProvider is missing');
  return state;
}
