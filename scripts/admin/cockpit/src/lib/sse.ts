export type SseStatus = 'idle' | 'connecting' | 'connected' | 'disconnected' | 'stopped';

export interface SseMessage<T = unknown> {
  data: T;
  raw: MessageEvent<string>;
}

export interface SseClientOptions<T = unknown> {
  url?: string;
  initialDelayMs?: number;
  maxDelayMs?: number;
  eventSourceFactory?: (url: string) => EventSource;
  onMessage?: (message: SseMessage<T>) => void;
  onStatus?: (status: SseStatus) => void;
  onError?: (error: Event) => void;
}

export interface SseClient {
  start(): void;
  stop(): void;
  getStatus(): SseStatus;
}

const DEFAULT_SSE_URL = '/api/sse/status';
const DEFAULT_INITIAL_DELAY_MS = 1_000;
const DEFAULT_MAX_DELAY_MS = 3_000;

function parseMessageData(raw: MessageEvent<string>): unknown {
  if (!raw.data) {
    return null;
  }
  try {
    return JSON.parse(raw.data);
  } catch {
    return raw.data;
  }
}

export function createSseClient<T = unknown>(options: SseClientOptions<T> = {}): SseClient {
  const createEventSource = options.eventSourceFactory ?? ((url: string) => new EventSource(url));
  const initialDelayMs = Math.max(0, options.initialDelayMs ?? DEFAULT_INITIAL_DELAY_MS);
  const maxDelayMs = Math.max(initialDelayMs, options.maxDelayMs ?? DEFAULT_MAX_DELAY_MS);
  let source: EventSource | null = null;
  let retryHandle: ReturnType<typeof setTimeout> | null = null;
  let retries = 0;
  let status: SseStatus = 'idle';
  let stopped = false;

  const setStatus = (next: SseStatus): void => {
    status = next;
    options.onStatus?.(status);
  };

  const cleanupSource = (): void => {
    if (source) {
      source.close();
      source = null;
    }
  };

  const clearRetry = (): void => {
    if (retryHandle) {
      clearTimeout(retryHandle);
      retryHandle = null;
    }
  };

  const connect = (): void => {
    if (stopped || source) {
      return;
    }
    setStatus('connecting');
    source = createEventSource(options.url ?? DEFAULT_SSE_URL);
    source.onopen = () => {
      retries = 0;
      setStatus('connected');
    };
    source.onmessage = (event) => {
      options.onMessage?.({
        data: parseMessageData(event) as T,
        raw: event
      });
    };
    source.onerror = (event) => {
      options.onError?.(event);
      cleanupSource();
      if (stopped) {
        return;
      }
      setStatus('disconnected');
      const delay = Math.min(maxDelayMs, initialDelayMs * (2 ** retries));
      retries += 1;
      clearRetry();
      retryHandle = setTimeout(() => {
        retryHandle = null;
        connect();
      }, delay);
    };
  };

  return {
    start() {
      stopped = false;
      clearRetry();
      if (!source) {
        connect();
      }
    },
    stop() {
      stopped = true;
      clearRetry();
      cleanupSource();
      setStatus('stopped');
    },
    getStatus() {
      return status;
    }
  };
}
