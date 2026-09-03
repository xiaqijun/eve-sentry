export type AlertLevel = "low" | "medium" | "high" | "critical";
export type OcrQueryType = "all" | "person" | "corporation" | "alliance";
export type OcrQueryStatus = "pending" | "running" | "partial" | "completed" | "failed" | "expired";
export type OcrNodeStatus = "pending" | "captured" | "timeout" | "ocr_failed" | "unavailable";

export interface SystemRef {
  name: string;
  system_id?: number | null;
}

export interface MonitoringNode {
  client_id: string;
  system_name: string;
  system_id?: number | null;
  [key: string]: unknown;
}

export interface MonitoringNodeChange {
  change: "online" | "offline" | "moved";
  node_id: string;
  system_name?: string;
  from_system?: string;
  to_system?: string;
  [key: string]: unknown;
}

export interface BootstrapPayload {
  schema_version: "intel_bootstrap.v1" | (string & {});
  generated_at: string;
  map: { systems?: Array<Record<string, unknown>>; summary?: Record<string, unknown>; [key: string]: unknown };
  alerts: Array<Record<string, unknown>>;
  active_intel: Array<Record<string, unknown>>;
  hostile_personnel?: HostileRoster[];
  clients: Record<string, unknown>;
  monitoring_nodes: MonitoringNode[];
  monitoring_nodes_version: string;
  monitoring_node_changes?: MonitoringNodeChange[];
  [key: string]: unknown;
}

export interface HostileRoster {
  system_name: string;
  system_id?: number | null;
  hostile_count: number;
  personnel: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface AlertEvent {
  id: string;
  system_name: string;
  system?: string;
  system_id?: number | null;
  names?: string[];
  character_ids?: number[];
  active_names?: string[];
  active_character_ids?: number[];
  classification?: string;
  hostile_count?: number;
  presence_only?: boolean;
  level: AlertLevel;
  score: number;
  created_at: string;
  seen_at?: string;
  source_observation_id: string;
  verified_characters: Array<Record<string, unknown>>;
  evidence?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface SafeEvent {
  system_name: string;
  system?: string;
  hostile_count: 0;
  active: false;
  created_at: string;
  message?: string;
  [key: string]: unknown;
}

export interface MonitoringNodeEvent {
  schema_version: "monitoring_node_event.v1" | (string & {});
  generated_at: string;
  changes: MonitoringNodeChange[];
  nodes_version: string;
  nodes: MonitoringNode[];
  [key: string]: unknown;
}

export interface HostileMovementEvent {
  schema_version: "hostile_movement_event.v1" | (string & {});
  movement_id: string;
  occurred_at: string;
  from_system: SystemRef;
  to_system: SystemRef;
  hostile_count: number;
  personnel: Array<Record<string, unknown>>;
  source?: "detector" | "bootstrap_reconciliation";
  [key: string]: unknown;
}

export type SseEventName = "bootstrap" | "alert" | "safe" | "monitoring_node" | "hostile_movement";

export interface OcrName {
  name: string;
  entity_id?: number | null;
  confidence?: number | null;
  [key: string]: unknown;
}

export interface OcrQueryCreateRequest {
  request_id: string;
  query_type: OcrQueryType;
  target_name?: string;
  target_id?: number | null;
  client_id?: string;
  source_instance?: string;
  window_ids?: string[];
  expires_in_seconds?: number;
  [key: string]: unknown;
}

export interface CaptureOcrOnceCommand {
  command: "capture_ocr_once";
  request_id: string;
  query_type: OcrQueryType;
  target_name?: string;
  target_id?: number | null;
  window_ids?: string[];
  expires_at: string;
  [key: string]: unknown;
}

export interface HeartbeatResponse {
  commands?: CaptureOcrOnceCommand[];
  [key: string]: unknown;
}

export interface OcrQueryResult {
  request_id: string;
  client_id: string;
  source_instance?: string;
  window_id: string;
  query_type: OcrQueryType;
  status: "success" | "ocr_failed";
  captured_at: string;
  names: OcrName[];
  error_code?: string;
  error_message?: string;
  [key: string]: unknown;
}

export interface OcrQueryNode {
  node_id: string;
  client_id: string;
  source_instance?: string;
  window_id: string;
  status: OcrNodeStatus;
  names?: OcrName[];
  error_code?: string;
  error_message?: string;
  captured_at?: string;
  [key: string]: unknown;
}

export interface OcrQueryAggregateResult {
  names: OcrName[];
  received_nodes: number;
  timed_out_nodes: number;
  failed_nodes: number;
  [key: string]: unknown;
}

export interface OcrQueryTask {
  request_id: string;
  task_id?: string;
  query_type: OcrQueryType;
  target_name?: string;
  target_id?: number | null;
  status: OcrQueryStatus;
  created_at: string;
  expires_at: string;
  nodes: OcrQueryNode[];
  result?: OcrQueryAggregateResult;
  error_code?: string;
  error_message?: string;
  [key: string]: unknown;
}
