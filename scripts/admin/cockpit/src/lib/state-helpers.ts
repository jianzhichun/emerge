import type { JsonObject } from './types';

export type StateKind = 'delta' | 'risk' | 'span' | 'exec-event' | 'pipeline-event';

export interface StateRow {
  key: string;
  kind: StateKind;
  ts: number;
  intent: string;
  title: string;
  status: string;
  data: JsonObject;
}

export function toText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean')
    return String(value);
  return '';
}

export function icon(kind: StateKind): string {
  if (kind === 'delta') return 'Δ';
  if (kind === 'risk') return '⚠';
  if (kind === 'span') return '◉';
  if (kind === 'exec-event') return 'E';
  if (kind === 'pipeline-event') return 'P';
  return '•';
}

export function label(kind: StateKind): string {
  if (kind === 'delta') return 'delta';
  if (kind === 'risk') return 'risk';
  if (kind === 'span') return 'span';
  if (kind === 'exec-event') return 'exec';
  if (kind === 'pipeline-event') return 'pipeline';
  return kind;
}

export function statusBadgeCls(status: string): string {
  const s = (status || '').toLowerCase();
  if (s === 'error' || s === 'failure' || s === 'retract') return 'critical';
  if (s === 'ok' || s === 'success' || s === 'handled') return 'stable';
  if (s === 'open') return 'open';
  if (s === 'provisional') return 'provisional';
  if (s === 'snoozed') return 'snoozed';
  return '';
}

export function rowStatusCls(status: string): string {
  const s = (status || '').toLowerCase();
  if (s === 'error' || s === 'failure' || s === 'retract') return 'critical';
  if (s === 'ok' || s === 'success') return 'stable';
  return '';
}

export function formatTime(ts: number): string {
  if (!ts) return '';
  return new Date(ts).toLocaleTimeString();
}

export function buildRows(payload: {
  deltas: JsonObject[];
  risks: JsonObject[];
  spans: JsonObject[];
  execEvents: JsonObject[];
  pipelineEvents: JsonObject[];
}): StateRow[] {
  const out: StateRow[] = [];
  let seq = 0;
  (payload.deltas || []).forEach((d) => {
    const key = `delta:${toText(d.id) || ++seq}`;
    out.push({
      key,
      kind: 'delta',
      ts: Number(d.ts_ms || 0),
      intent: String(d.intent_signature || ''),
      title: String(d.message || '(no message)'),
      status: String(d.reconcile_outcome || (d.provisional ? 'provisional' : 'open')),
      data: d,
    });
  });
  (payload.risks || []).forEach((r) => {
    const key = `risk:${toText(r.risk_id) || ++seq}`;
    out.push({
      key,
      kind: 'risk',
      ts: Number(r.created_at_ms || 0),
      intent: String(r.intent_signature || ''),
      title: String(r.text || '(no risk text)'),
      status: String(r.status || 'open'),
      data: r,
    });
  });
  (payload.spans || []).forEach((s) => {
    const key = `span:${toText(s.span_id) || `${s.closed_at_ms || 0}:${toText(s.intent_signature)}:${++seq}`}`;
    out.push({
      key,
      kind: 'span',
      ts: Number(s.closed_at_ms || s.opened_at_ms || 0),
      intent: String(s.intent_signature || ''),
      title: String(s.description || 'span'),
      status: String(s.outcome || 'unknown'),
      data: s,
    });
  });
  (payload.execEvents || []).forEach((e, i) => {
    const key = `exec-event:${i}:${e.ts_ms || 0}:${toText(e.intent_signature)}`;
    out.push({
      key,
      kind: 'exec-event',
      ts: Number(e.ts_ms || 0),
      intent: String(e.intent_signature || ''),
      title: String(e.mode || 'exec'),
      status: e.is_error ? 'error' : 'ok',
      data: e,
    });
  });
  (payload.pipelineEvents || []).forEach((e, i) => {
    const pipeId = toText(e.pipeline_id) || toText(e.intent_signature) || '';
    const key = `pipeline-event:${i}:${e.ts_ms || 0}:${pipeId}`;
    out.push({
      key,
      kind: 'pipeline-event',
      ts: Number(e.ts_ms || 0),
      intent: String(e.intent_signature || pipeId),
      title: String(e.pipeline_id || 'pipeline-event'),
      status: e.is_error ? 'error' : 'ok',
      data: e,
    });
  });
  out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return out;
}

export function relatedRows(all: StateRow[], selected: StateRow | null): StateRow[] {
  if (!selected) return [];
  const intent = String(selected.intent || '').trim();
  if (intent) return all.filter((r) => String(r.intent || '') === intent);
  const titlePrefix = String(selected.title || '').trim().slice(0, 32).toLowerCase();
  if (!titlePrefix) return [selected];
  return all.filter(
    (r) => r.kind === selected.kind && String(r.title || '').toLowerCase().includes(titlePrefix)
  );
}
