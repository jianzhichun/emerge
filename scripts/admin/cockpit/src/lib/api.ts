import type {
  AssetsResponse,
  EventListResponse,
  EventQuery,
  GoalResponse,
  GoalSetRequest,
  MonitorsResponse,
  PolicyResponse,
  RunnerEventsRequest,
  RunnerEventsResponse,
  SessionExportResponse,
  SessionResetRequest,
  SessionResetResponse,
  SessionResponse,
  SessionsResponse,
  StatusResponse,
  StateResponse
} from './types';

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  sessionId?: string;
  signal?: AbortSignal;
}

export class ApiRequestError extends Error {
  status: number;
  path: string;
  details?: unknown;

  constructor(message: string, path: string, status = 0, details?: unknown) {
    super(message);
    this.name = 'ApiRequestError';
    this.path = path;
    this.status = status;
    this.details = details;
  }
}

function withQuery(path: string, query: RequestOptions['query'], sessionId?: string): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') {
      continue;
    }
    params.set(key, String(value));
  }
  if (sessionId) {
    params.set('session_id', sessionId);
  }
  const suffix = params.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function buildAbsoluteUrl(baseUrl: string | undefined, pathWithQuery: string): string {
  if (!baseUrl) {
    return pathWithQuery;
  }
  return new URL(pathWithQuery, baseUrl).toString();
}

async function parseJsonOrThrow(response: Response, path: string): Promise<unknown> {
  const raw = await response.text();
  if (!raw.trim()) {
    throw new ApiRequestError('Empty JSON response body', path, response.status);
  }
  try {
    return JSON.parse(raw);
  } catch {
    throw new ApiRequestError('Malformed JSON response body', path, response.status, raw.slice(0, 200));
  }
}

async function parseErrorDetails(response: Response): Promise<unknown> {
  const raw = await response.text();
  if (!raw.trim()) {
    return undefined;
  }
  try {
    return JSON.parse(raw);
  } catch {
    return raw.slice(0, 400);
  }
}

export function createApiClient(options: ApiClientOptions = {}) {
  const fetchImpl = options.fetchImpl ?? fetch;

  async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
    const method = init.method ?? 'GET';
    const pathWithQuery = withQuery(path, init.query, init.sessionId);
    const url = buildAbsoluteUrl(options.baseUrl, pathWithQuery);
    let response: Response;
    try {
      response = await fetchImpl(url, {
        method,
        signal: init.signal,
        headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: init.body === undefined ? undefined : JSON.stringify(init.body)
      });
    } catch (error) {
      throw new ApiRequestError(
        'Network request failed',
        pathWithQuery,
        0,
        error instanceof Error ? error.message : error
      );
    }

    if (!response.ok) {
      const details = await parseErrorDetails(response);
      throw new ApiRequestError(`Request failed: ${response.status} ${response.statusText}`, pathWithQuery, response.status, details);
    }
    return (await parseJsonOrThrow(response, pathWithQuery)) as T;
  }

  return {
    request,
    getStatus: () =>
      request<StatusResponse>('/api/status'),
    getPolicy: (sessionId?: string) =>
      request<PolicyResponse>('/api/policy', { sessionId }),
    getMonitors: () =>
      request<MonitorsResponse>('/api/control-plane/monitors'),
    getRunnerEvents: ({ profile, limit = 20 }: RunnerEventsRequest) =>
      request<RunnerEventsResponse>('/api/control-plane/runner-events', {
        query: { profile, limit }
      }),
    getSession: (sessionId?: string) =>
      request<SessionResponse>('/api/control-plane/session', { sessionId }),
    getExecEvents: (query: EventQuery = {}) =>
      request<EventListResponse>('/api/control-plane/exec-events', {
        sessionId: query.sessionId,
        query: {
          limit: query.limit,
          since_ms: query.sinceMs,
          intent: query.intent,
          intent_prefix: query.intentPrefix
        }
      }),
    getToolEvents: (query: EventQuery = {}) =>
      request<EventListResponse>('/api/control-plane/tool-events', {
        sessionId: query.sessionId,
        query: {
          limit: query.limit,
          since_ms: query.sinceMs
        }
      }),
    getPipelineEvents: (query: EventQuery = {}) =>
      request<EventListResponse>('/api/control-plane/pipeline-events', {
        sessionId: query.sessionId,
        query: {
          limit: query.limit,
          since_ms: query.sinceMs,
          intent: query.intent,
          intent_prefix: query.intentPrefix
        }
      }),
    getState: () =>
      request<StateResponse>('/api/control-plane/state'),
    getSessions: () =>
      request<SessionsResponse>('/api/control-plane/sessions'),
    exportSession: (sessionId?: string) =>
      request<SessionExportResponse>('/api/control-plane/session/export', {
        method: 'POST',
        sessionId
      }),
    resetSession: (payload: SessionResetRequest = {}, sessionId?: string) =>
      request<SessionResetResponse>('/api/control-plane/session/reset', {
        method: 'POST',
        sessionId,
        body: {
          confirm: payload.confirm ?? 'RESET',
          full: Boolean(payload.full)
        }
      }),
    getAssets: () =>
      request<AssetsResponse>('/api/assets'),
    getGoal: () =>
      request<GoalResponse>('/api/goal'),
    postGoal: (payload: GoalSetRequest, endpoint = '/api/goal') =>
      request<GoalResponse>(endpoint, {
        method: 'POST',
        body: payload
      })
  };
}

export const api = createApiClient();
