import { writable } from 'svelte/store';

export interface QueueDraft {
  type: string;
  label: string;
  subLabel: string;
  command: string;
  data: Record<string, unknown>;
}

export interface QueueItem extends QueueDraft {
  id: number;
}

export interface QueueState {
  items: QueueItem[];
  submitting: boolean;
  _idSeq: number;
}

export interface QueueStore {
  subscribe: ReturnType<typeof writable<QueueState>>['subscribe'];
  enqueue(draft: QueueDraft): void;
  dequeue(id: number): void;
  clear(): void;
}

export function createQueueStore(): QueueStore {
  let _state: QueueState = { items: [], submitting: false, _idSeq: 0 };
  const { subscribe, set } = writable<QueueState>(_state);

  function _set(next: QueueState): void {
    _state = next;
    set(next);
  }

  return {
    subscribe,
    enqueue(draft: QueueDraft): void {
      const id = _state._idSeq + 1;
      _set({ ..._state, _idSeq: id, items: [..._state.items, { id, ...draft }] });
    },
    dequeue(id: number): void {
      _set({ ..._state, items: _state.items.filter((item) => item.id !== id) });
    },
    clear(): void {
      _set({ ..._state, items: [] });
    },
  };
}

export const queueStore = createQueueStore();
