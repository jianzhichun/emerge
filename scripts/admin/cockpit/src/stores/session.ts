import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { HookStateResponse, SessionResponse, SessionSummary, SessionsResponse } from '../lib/types';

export interface SessionStoreState {
  loading: boolean;
  error: string | null;
  currentSessionId: string | null;
  sessions: SessionSummary[];
  session: SessionResponse | null;
  hookPlane: HookStateResponse | null;
}

const initialState: SessionStoreState = {
  loading: false,
  error: null,
  currentSessionId: null,
  sessions: [],
  session: null,
  hookPlane: null
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

function createSessionStore() {
  const { subscribe, update } = writable<SessionStoreState>(initialState);

  return {
    subscribe,
    refresh: async (sessionId?: string): Promise<{ sessions: SessionsResponse; session: SessionResponse }> => {
      update((state) => ({ ...state, loading: true, error: null }));
      try {
        const sessionsPayload = await api.getSessions();
        const targetSessionId = sessionId ?? sessionsPayload.current_session_id;
        const sessionPayload = await api.getSession(targetSessionId);
        let hookPlane: HookStateResponse | null = null;
        try {
          hookPlane = await api.getHookState();
        } catch {
          hookPlane = null;
        }
        update((state) => ({
          ...state,
          loading: false,
          error: null,
          currentSessionId: sessionsPayload.current_session_id ?? null,
          sessions: sessionsPayload.sessions ?? [],
          session: sessionPayload,
          hookPlane
        }));
        return { sessions: sessionsPayload, session: sessionPayload };
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

export const sessionStore = createSessionStore();
