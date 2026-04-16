import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { createQueueStore, type QueueDraft } from './queue';

const draft: QueueDraft = {
  type: 'pipeline',
  label: 'Run pipeline',
  subLabel: 'foo.read.main',
  command: 'run-pipeline',
  data: { key: 'foo.read.main' },
};

describe('queueStore', () => {
  it('starts empty', () => {
    const store = createQueueStore();
    expect(get(store).items).toEqual([]);
    expect(get(store).submitting).toBe(false);
  });

  it('enqueues items with incrementing ids', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue({ ...draft, label: 'Second' });
    const { items } = get(store);
    expect(items).toHaveLength(2);
    expect(items[0].id).toBe(1);
    expect(items[1].id).toBe(2);
  });

  it('dequeues by id', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue({ ...draft, label: 'Second' });
    const firstId = get(store).items[0].id;
    store.dequeue(firstId);
    expect(get(store).items).toHaveLength(1);
    expect(get(store).items[0].label).toBe('Second');
  });

  it('clears all items', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    store.enqueue(draft);
    store.clear();
    expect(get(store).items).toEqual([]);
  });

  it('preserves item data property', () => {
    const store = createQueueStore();
    store.enqueue(draft);
    expect(get(store).items[0].data).toEqual({ key: 'foo.read.main' });
  });
});
