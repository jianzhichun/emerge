import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { PolicyPipeline, PolicyResponse, PolicyThresholds } from '../lib/types';

export interface PolicyStoreState {
  loading: boolean;
  error: string | null;
  lastUpdatedMs: number | null;
  raw: PolicyResponse | null;
  goal: string | null;
  thresholds: PolicyThresholds;
  pipelines: PolicyPipeline[];
  queueLength: number;
}

const initialState: PolicyStoreState = {
  loading: false,
  error: null,
  lastUpdatedMs: null,
  raw: null,
  goal: null,
  thresholds: {},
  pipelines: [],
  queueLength: 0
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
  const { subscribe, update, set } = writable<PolicyStoreState>(initialState);

  return {
    subscribe,
    reset: () => set(initialState),
    refresh: async (sessionId?: string): Promise<PolicyResponse> => {
      update((state) => ({ ...state, loading: true, error: null }));
      try {
        const payload = await api.getPolicy(sessionId);
        update((state) => ({
          ...state,
          loading: false,
          error: null,
          lastUpdatedMs: Date.now(),
          raw: payload,
          goal: payload.goal ?? null,
          thresholds: payload.thresholds ?? {},
          pipelines: payload.pipelines ?? [],
          queueLength: Number(payload.pipeline_count ?? payload.pipelines?.length ?? 0)
        }));
        return payload;
      } catch (error) {
        update((state) => ({
          ...state,
          loading: false,
          error: toErrorMessage(error)
        }));
        throw error;
      }
    }
  };
}

export const policyStore = createPolicyStore();
