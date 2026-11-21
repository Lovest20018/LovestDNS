import { createContext, useContext } from 'react';
import type { GatewayConfig, GatewaySummary } from './gatewayApi';

export type GatewayConnectionTest = { revision: string; url: string; summary: GatewaySummary };
export interface GatewayState {
  connectionTest: GatewayConnectionTest | null;
  setConnectionTest: (test: GatewayConnectionTest | null) => void;
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
