import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { GoalResponse } from '../lib/types';

export interface GoalStoreState {
  loading: boolean;
  error: string | null;
  lastUpdatedMs: number | null;
  active: GoalResponse | null;
  history: GoalResponse[];
}

const initialState: GoalStoreState = {
  loading: false,
  error: null,
  lastUpdatedMs: null,
  active: null,
  history: []
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

function sameGoal(a: GoalResponse | null, b: GoalResponse): boolean {
  if (!a) {
    return false;
  }
  return a.goal === b.goal && a.goal_version === b.goal_version;
}

function createGoalStore() {
  const { subscribe, update, set } = writable<GoalStoreState>(initialState);

  return {
    subscribe,
    reset: () => set(initialState),
    refresh: async (): Promise<GoalResponse> => {
      update((state) => ({ ...state, loading: true, error: null }));
      try {
        const payload = await api.getGoal();
        update((state) => ({
          ...state,
          loading: false,
          error: null,
          lastUpdatedMs: Date.now(),
          active: payload,
          history: sameGoal(state.active, payload) ? state.history : [payload, ...state.history].slice(0, 10)
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

export const goalStore = createGoalStore();
