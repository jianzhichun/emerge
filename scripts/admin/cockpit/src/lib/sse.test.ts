import { afterEach, describe, expect, it, vi } from 'vitest';

import { createSseClient, type SseStatus } from './sse';

class MockEventSource {
  url: string;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
  }

  close(): void {
    this.closed = true;
  }

  emitOpen(): void {
    this.onopen?.({} as Event);
  }

  emitError(): void {
    this.onerror?.({} as Event);
  }
}

describe('sse client', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('transitions start -> connecting -> connected', () => {
    const statuses: SseStatus[] = [];
    const sources: MockEventSource[] = [];
    const client = createSseClient({
      eventSourceFactory: (url) => {
        const source = new MockEventSource(url);
        sources.push(source);
        return source as unknown as EventSource;
      },
      onStatus: (status) => statuses.push(status)
    });

    client.start();
    expect(statuses).toEqual(['connecting']);
    expect(sources).toHaveLength(1);

    sources[0].emitOpen();
    expect(statuses).toEqual(['connecting', 'connected']);
    expect(client.getStatus()).toBe('connected');
  });

  it('schedules reconnect with backoff after onerror', () => {
    vi.useFakeTimers();

    const sources: MockEventSource[] = [];
    const client = createSseClient({
      initialDelayMs: 1000,
      maxDelayMs: 3000,
      eventSourceFactory: (url) => {
        const source = new MockEventSource(url);
        sources.push(source);
        return source as unknown as EventSource;
      }
    });

    client.start();
    expect(sources).toHaveLength(1);

    sources[0].emitError();
    expect(sources[0].closed).toBe(true);
    expect(client.getStatus()).toBe('disconnected');

    vi.advanceTimersByTime(999);
    expect(sources).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(sources).toHaveLength(2);
    expect(client.getStatus()).toBe('connecting');
  });

  it('stop cancels pending reconnect and sets status stopped', () => {
    vi.useFakeTimers();

    const sources: MockEventSource[] = [];
    const client = createSseClient({
      initialDelayMs: 1000,
      eventSourceFactory: (url) => {
        const source = new MockEventSource(url);
        sources.push(source);
        return source as unknown as EventSource;
      }
    });

    client.start();
    sources[0].emitError();
    expect(client.getStatus()).toBe('disconnected');

    client.stop();
    expect(client.getStatus()).toBe('stopped');

    vi.advanceTimersByTime(5000);
    expect(sources).toHaveLength(1);
    expect(sources[0].closed).toBe(true);
  });
});
