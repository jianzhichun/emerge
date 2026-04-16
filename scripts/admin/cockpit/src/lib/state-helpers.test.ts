import { describe, expect, it } from 'vitest';
import {
  buildRows,
  icon,
  label,
  relatedRows,
  rowStatusCls,
  statusBadgeCls,
  toText,
  formatTime,
  type StateRow,
} from './state-helpers';

describe('toText', () => {
  it('returns empty string for null/undefined', () => {
    expect(toText(null)).toBe('');
    expect(toText(undefined)).toBe('');
  });
  it('converts primitives to string', () => {
    expect(toText('hello')).toBe('hello');
    expect(toText(42)).toBe('42');
    expect(toText(true)).toBe('true');
  });
  it('returns empty string for objects', () => {
    expect(toText({ a: 1 })).toBe('');
  });
});

describe('icon', () => {
  it('returns correct icons', () => {
    expect(icon('delta')).toBe('Δ');
    expect(icon('risk')).toBe('⚠');
    expect(icon('span')).toBe('◉');
    expect(icon('exec-event')).toBe('E');
    expect(icon('pipeline-event')).toBe('P');
  });
});

describe('label', () => {
  it('returns correct labels', () => {
    expect(label('delta')).toBe('delta');
    expect(label('exec-event')).toBe('exec');
    expect(label('pipeline-event')).toBe('pipeline');
  });
});

describe('statusBadgeCls', () => {
  it('maps error states to critical', () => {
    expect(statusBadgeCls('error')).toBe('critical');
    expect(statusBadgeCls('failure')).toBe('critical');
    expect(statusBadgeCls('retract')).toBe('critical');
  });
  it('maps success states to stable', () => {
    expect(statusBadgeCls('ok')).toBe('stable');
    expect(statusBadgeCls('handled')).toBe('stable');
  });
  it('handles open/provisional/snoozed', () => {
    expect(statusBadgeCls('open')).toBe('open');
    expect(statusBadgeCls('provisional')).toBe('provisional');
    expect(statusBadgeCls('snoozed')).toBe('snoozed');
  });
  it('is case-insensitive', () => {
    expect(statusBadgeCls('ERROR')).toBe('critical');
  });
  it('returns empty string for unknown', () => {
    expect(statusBadgeCls('unknown')).toBe('');
  });
});

describe('rowStatusCls', () => {
  it('maps error to critical, ok to stable', () => {
    expect(rowStatusCls('error')).toBe('critical');
    expect(rowStatusCls('ok')).toBe('stable');
    expect(rowStatusCls('open')).toBe('');
  });
});

describe('buildRows', () => {
  it('builds delta rows from payload', () => {
    const rows = buildRows({
      deltas: [{ id: 'd1', message: 'test delta', intent_signature: 'foo', ts_ms: 1000 }],
      risks: [],
      spans: [],
      execEvents: [],
      pipelineEvents: [],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('delta');
    expect(rows[0].key).toBe('delta:d1');
    expect(rows[0].title).toBe('test delta');
    expect(rows[0].intent).toBe('foo');
  });

  it('sorts rows by ts descending', () => {
    const rows = buildRows({
      deltas: [
        { id: 'old', message: 'old', ts_ms: 100 },
        { id: 'new', message: 'new', ts_ms: 200 },
      ],
      risks: [], spans: [], execEvents: [], pipelineEvents: [],
    });
    expect(rows[0].key).toBe('delta:new');
    expect(rows[1].key).toBe('delta:old');
  });

  it('marks unreconciled delta status as open', () => {
    const rows = buildRows({
      deltas: [{ id: 'd1', message: 'x' }],
      risks: [], spans: [], execEvents: [], pipelineEvents: [],
    });
    expect(rows[0].status).toBe('open');
  });
});

describe('relatedRows', () => {
  const rows: StateRow[] = [
    { key: 'delta:a', kind: 'delta', ts: 0, intent: 'foo.bar', title: 'A', status: 'open', data: {} },
    { key: 'risk:b', kind: 'risk', ts: 0, intent: 'foo.bar', title: 'B', status: 'open', data: {} },
    { key: 'span:c', kind: 'span', ts: 0, intent: 'other', title: 'C', status: 'ok', data: {} },
  ];

  it('returns empty array when selected is null', () => {
    expect(relatedRows(rows, null)).toEqual([]);
  });

  it('returns rows with the same intent', () => {
    const related = relatedRows(rows, rows[0]);
    expect(related.map((r) => r.key)).toEqual(['delta:a', 'risk:b']);
  });
});

describe('formatTime', () => {
  it('returns empty string for falsy ts', () => {
    expect(formatTime(0)).toBe('');
  });
  it('returns a string for valid ts', () => {
    const result = formatTime(1_000_000_000);
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });
});
