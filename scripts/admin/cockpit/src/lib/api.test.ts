import { describe, expect, it, vi } from 'vitest';

import { ApiRequestError, createApiClient } from './api';

type FetchMock = ReturnType<typeof vi.fn>;

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' }
  });
}

describe('api client', () => {
  it('calls /api/status', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ ok: true, pending: false, server_online: true }));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    await api.getStatus();

    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:8789/api/status',
      expect.objectContaining({ method: 'GET' })
    );
  });

  it('calls /api/policy', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ pipeline_count: 1 }));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    await api.getPolicy();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://localhost:8789/api/policy',
      expect.objectContaining({ method: 'GET' })
    );
  });

  it('builds runner-events query parameters', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ ok: true, events: [] }));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    await api.getRunnerEvents({ profile: 'my-runner', limit: 50 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://localhost:8789/api/control-plane/runner-events?profile=my-runner&limit=50');
  });

  it('uses default runner-events limit when omitted', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ ok: true, events: [] }));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    await api.getRunnerEvents({ profile: 'runner-default' });

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://localhost:8789/api/control-plane/runner-events?profile=runner-default&limit=20');
  });

  it('adds session_id query for session export post', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ ok: true, snapshot: {} }));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    await api.exportSession('session-abc');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://localhost:8789/api/control-plane/session/export?session_id=session-abc');
    expect(init.method).toBe('POST');
  });

  it('throws ApiRequestError on non-2xx response', async () => {
    const fetchMock: FetchMock = vi.fn(async () => jsonResponse({ error: 'boom' }, 500));
    const api = createApiClient({ baseUrl: 'http://localhost:8789', fetchImpl: fetchMock as typeof fetch });

    const request = api.getState();
    await expect(request).rejects.toBeInstanceOf(ApiRequestError);
    await expect(request).rejects.toMatchObject({
      status: 500,
      path: '/api/control-plane/state'
    });
  });
});
