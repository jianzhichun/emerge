import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { DeltaItem, RiskItem, StateResponse } from '../lib/types';

export interface StateStoreState {
  error: string | null;
  verificationState: string | null;
  deltas: DeltaItem[];
  risks: RiskItem[];
  activeSpanId: string | null;
  activeSpanIntent: string | null;
}

const initialState: StateStoreState = {
  error: null,
  verificationState: null,
  deltas: [],
  risks: [],
  activeSpanId: null,
  activeSpanIntent: null
};

function toErrorMessage(error: unknown): string {
  if (error instanceof ApiRequestError) {
    return `${error.message} (${error.path})`;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function createStateStore() {
  const { subscribe, update } = writable<StateStoreState>(initialState);

  return {
    subscribe,
    refresh: async (): Promise<StateResponse> => {
      try {
        const payload = await api.getState();
        update((state) => ({
          ...state,
          error: null,
          verificationState: payload.verification_state ?? null,
          deltas: payload.deltas ?? [],
          risks: payload.risks ?? [],
          activeSpanId: payload.active_span_id ?? null,
          activeSpanIntent: payload.active_span_intent ?? null
        }));
        return payload;
      } catch (error) {
        update((state) => ({
          ...state,
          error: toErrorMessage(error)
        }));
        throw error;
      }
    }
  };
}

export const stateStore = createStateStore();
