import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { PolicyIntent, PolicyResponse, PolicyThresholds } from '../lib/types';

export interface PolicyStoreState {
  error: string | null;
  thresholds: PolicyThresholds;
  intents: PolicyIntent[];
}

const initialState: PolicyStoreState = {
  error: null,
  thresholds: {},
  intents: []
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

function createPolicyStore() {
  const { subscribe, update } = writable<PolicyStoreState>(initialState);

  return {
    subscribe,
    refresh: async (sessionId?: string): Promise<PolicyResponse> => {
      try {
        const payload = await api.getPolicy(sessionId);
        update((state) => ({
          ...state,
          error: null,
          thresholds: payload.thresholds ?? {},
          intents: payload.intents ?? []
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

export const policyStore = createPolicyStore();
