import { writable } from 'svelte/store';
import { api, ApiRequestError } from '../lib/api';
import type { SessionResponse, SessionSummary, SessionsResponse } from '../lib/types';

export interface SessionStoreState {
  loading: boolean;
  error: string | null;
  lastUpdatedMs: number | null;
  currentSessionId: string | null;
  sessions: SessionSummary[];
  session: SessionResponse | null;
  hookState: Record<string, unknown> | null;
}

const initialState: SessionStoreState = {
  loading: false,
  error: null,
  lastUpdatedMs: null,
  currentSessionId: null,
  sessions: [],
  session: null,
  hookState: null
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

function normalizeHookState(session: SessionResponse): Record<string, unknown> | null {
  if (!session.recovery || typeof session.recovery !== 'object') {
    return null;
  }
  return session.recovery;
}

function createSessionStore() {
  const { subscribe, update, set } = writable<SessionStoreState>(initialState);

  return {
    subscribe,
    reset: () => set(initialState),
    refresh: async (sessionId?: string): Promise<{ sessions: SessionsResponse; session: SessionResponse }> => {
      update((state) => ({ ...state, loading: true, error: null }));
      try {
        const sessionsPayload = await api.getSessions();
        const targetSessionId = sessionId ?? sessionsPayload.current_session_id;
        const sessionPayload = await api.getSession(targetSessionId);
        update((state) => ({
          ...state,
          loading: false,
          error: null,
          lastUpdatedMs: Date.now(),
          currentSessionId: sessionsPayload.current_session_id ?? null,
          sessions: sessionsPayload.sessions ?? [],
          session: sessionPayload,
          hookState: normalizeHookState(sessionPayload)
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
