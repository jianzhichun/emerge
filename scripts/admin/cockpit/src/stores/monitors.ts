import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { MonitorRunner, MonitorsResponse, RunnerEventsResponse } from '../lib/types';

export interface MonitorsStoreState {
  loading: boolean;
  error: string | null;
  lastUpdatedMs: number | null;
  runners: MonitorRunner[];
  teamActive: boolean;
  expandedFeeds: Record<string, boolean>;
  recentByProfile: Record<string, RunnerEventsResponse>;
  feedErrorByProfile: Record<string, string | null>;
}

const initialState: MonitorsStoreState = {
  loading: false,
  error: null,
  lastUpdatedMs: null,
  runners: [],
  teamActive: false,
  expandedFeeds: {},
  recentByProfile: {},
  feedErrorByProfile: {}
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

function createMonitorsStore() {
  const { subscribe, update, set } = writable<MonitorsStoreState>(initialState);

  return {
    subscribe,
    reset: () => set(initialState),
    refresh: async (): Promise<MonitorsResponse> => {
      update((state) => ({ ...state, loading: true, error: null }));
      try {
        const payload = await api.getMonitors();
        update((state) => ({
          ...state,
          loading: false,
          error: null,
          lastUpdatedMs: Date.now(),
          runners: payload.runners ?? [],
          teamActive: Boolean(payload.team_active)
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
    },
    refreshRunnerEvents: async (profile: string, limit = 20): Promise<RunnerEventsResponse> => {
      try {
        const payload = await api.getRunnerEvents({ profile, limit });
        update((state) => ({
          ...state,
          feedErrorByProfile: {
            ...state.feedErrorByProfile,
            [profile]: null
          },
          recentByProfile: {
            ...state.recentByProfile,
            [profile]: payload
          }
        }));
        return payload;
      } catch (error) {
        update((state) => ({
          ...state,
          feedErrorByProfile: {
            ...state.feedErrorByProfile,
            [profile]: toErrorMessage(error)
          }
        }));
        throw error;
      }
    },
    toggleFeed: (profile: string) =>
      update((state) => ({
        ...state,
        expandedFeeds: {
          ...state.expandedFeeds,
          [profile]: !state.expandedFeeds[profile]
        }
      }))
  };
}

export const monitorsStore = createMonitorsStore();
