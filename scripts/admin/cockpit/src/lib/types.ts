export type JsonObject = Record<string, unknown>;

export interface ApiOkResponse {
  ok?: boolean;
  error?: string;
  message?: string;
}

export interface EventQuery {
  limit?: number;
  sinceMs?: number;
  intent?: string;
  intentPrefix?: string;
  sessionId?: string;
}

export interface PolicyThresholds {
  promote_min_attempts?: number;
  promote_min_success_rate?: number;
  promote_min_verify_rate?: number;
  promote_max_human_fix_rate?: number;
  stable_min_attempts?: number;
  stable_min_success_rate?: number;
  stable_min_verify_rate?: number;
  rollback_consecutive_failures?: number;
}

export interface TransitionHistoryEntry {
  ts_ms?: number;
  from_stage?: string;
  to_stage?: string;
  reason?: string;
  attempts?: number;
  success_rate?: number;
  verify_rate?: number;
  human_fix_rate?: number;
  window_success_rate?: number;
  consecutive_failures?: number;
  session_id?: string | null;
  target_profile?: string | null;
  execution_path?: string | null;
}

export interface PolicyIntent extends JsonObject {
  key?: string;
  stage?: string;
  rollout_pct?: number;
  frozen?: boolean;
  updated_at_ms?: number;
  success_rate?: number;
  verify_rate?: number;
  human_fix_rate?: number;
  attempts?: number;
  successes?: number;
  consecutive_failures?: number;
  last_transition_reason?: string | null;
  last_transition_ts_ms?: number;
  last_demotion?: TransitionHistoryEntry | null;
}

export interface IntentHistoryResponse extends ApiOkResponse {
  intent_signature?: string;
  stage?: string;
  last_transition_reason?: string | null;
  last_transition_ts_ms?: number;
  last_demotion?: TransitionHistoryEntry | null;
  transition_history?: TransitionHistoryEntry[];
}

export interface PolicyResponse extends ApiOkResponse {
  session_id?: string;
  state_root?: string;
  registry_exists?: boolean;
  registry_corrupt?: boolean;
  intent_count?: number;
  thresholds?: PolicyThresholds;
  intents?: PolicyIntent[];
}

export interface MonitorRunner {
  runner_profile?: string;
  connected?: boolean;
  connected_at_ms?: number;
  last_event_ts_ms?: number;
  machine_id?: string;
  last_alert?: JsonObject | null;
}

export interface MonitorsResponse {
  runners?: MonitorRunner[];
  team_active?: boolean;
}

export interface StatusResponse extends ApiOkResponse {
  pending?: boolean;
  cc_active?: boolean;
  last_cockpit_event_id?: string | null;
  last_cockpit_event_ts_ms?: number | null;
  last_cockpit_ack_event_id?: string | null;
  last_cockpit_ack_ts_ms?: number | null;
  cockpit_ack_pending?: boolean;
  cockpit_ack_lag_ms?: number | null;
  watchers_healthy?: boolean;
  watchers_alive_count?: number;
  watchers_total?: number;
  watchers_stale_ids?: string[];
}

export interface WatcherStatus {
  watcher_id?: string;
  pid?: number | null;
  target?: string | null;
  state_root?: string | null;
  started_at_ms?: number | null;
  last_loop_ts_ms?: number | null;
  updated_at_ms?: number | null;
  stopped_at_ms?: number | null;
  events_read?: number;
  events_delivered?: number;
  last_event_id?: string | null;
  last_error?: JsonObject | null;
  consecutive_errors?: number;
  alive?: boolean;
  lag_ms?: number | null;
  stale_threshold_ms?: number;
}

export interface WatchersResponse extends ApiOkResponse {
  total?: number;
  alive_count?: number;
  stale_count?: number;
  healthy?: boolean;
  stale_watcher_ids?: string[];
  watchers?: WatcherStatus[];
  stale_threshold_s?: number;
}

export interface RunnerEvent extends JsonObject {
  ts_ms?: number;
  type?: string;
}

export interface RunnerEventsResponse extends ApiOkResponse {
  events?: RunnerEvent[];
  activity?: number[];
  today_events?: number;
  today_alerts?: number;
}

export interface RunnerEventsRequest {
  profile: string;
  limit?: number;
}

export interface SessionSummary extends JsonObject {
  session_id?: string;
  last_ts_ms?: number;
  has_checkpoint?: boolean;
  has_wal?: boolean;
}

export interface SessionsResponse extends ApiOkResponse {
  current_session_id?: string;
  sessions?: SessionSummary[];
}

export interface SessionResponse extends ApiOkResponse {
  session_id?: string;
  session_dir?: string;
  wal_entries?: number;
  checkpoint?: JsonObject | null;
  recovery?: JsonObject | null;
}

/** `GET /api/control-plane/hook-state` — global hook fields + context preview (legacy Session tab). */
export interface HookFields {
  turn_count?: number;
  active_span_id?: string | null;
  active_span_intent?: string | null;
  span_nudge_sent?: boolean;
}

export interface RegisteredHookEntry {
  event?: string;
  command?: string;
}

export interface HookStateResponse extends ApiOkResponse {
  hook_fields?: HookFields;
  context_preview?: string;
  registered_hooks?: RegisteredHookEntry[];
}

export interface EventListResponse<TEvent = JsonObject> extends ApiOkResponse {
  events?: TEvent[];
}

export interface DeltaItem extends JsonObject {
  id?: string;
  message?: string;
  level?: string;
  verification_state?: string;
  provisional?: boolean;
  intent_signature?: string | null;
  tool_name?: string | null;
  ts_ms?: number;
}

export interface RiskItem extends JsonObject {
  risk_id?: string;
  text?: string;
  status?: string;
  created_at_ms?: number;
  snoozed_until_ms?: number | null;
  handled_reason?: string | null;
  source_delta_id?: string | null;
  intent_signature?: string | null;
}

export interface StateResponse extends ApiOkResponse {
  deltas?: DeltaItem[];
  risks?: RiskItem[];
  verification_state?: string;
  consistency_window_ms?: number;
  active_span_id?: string | null;
  active_span_intent?: string | null;
}

export interface SessionExportResponse extends ApiOkResponse {
  snapshot?: JsonObject;
}

export interface SessionResetRequest {
  confirm?: string;
  full?: boolean;
}

export interface SessionResetResponse extends ApiOkResponse {
  reset?: boolean;
  full?: boolean;
  removed_paths?: string[];
  pre_reset_snapshot?: JsonObject;
}

export interface AssetComponent {
  filename?: string;
  context?: string;
}

export interface AssetConnector {
  notes?: string | null;
  components?: AssetComponent[];
}

export interface AssetsResponse {
  connectors?: Record<string, AssetConnector>;
}

export interface ActionSchemaProperty {
  type?: string;
}

export interface ActionSchema {
  required?: string[];
  properties?: Record<string, ActionSchemaProperty>;
}

export interface ActionTypeEntry {
  type: string;
  hazard?: 'safe' | 'write' | 'danger' | string;
  description?: string;
  schema?: ActionSchema;
}

export interface ActionTypesResponse extends ApiOkResponse {
  types?: ActionTypeEntry[];
}
