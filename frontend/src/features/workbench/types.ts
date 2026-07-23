export type Level = "low" | "medium" | "high" | "critical";

export interface MapSystem {
  name: string;
  system_id?: number;
  x: number;
  y: number;
  hostile_count?: number;
  report_count?: number;
  kill_count?: number;
  loss_count?: number;
  recent_kill_count?: number;
  latest_kill_at?: string | null;
  hostiles?: string[];
  [key: string]: unknown;
}

export interface MapLink {
  from: string;
  to: string;
}

export interface MapSummary {
  system_count?: number;
  hostile_count?: number;
  report_count?: number;
  alert_count?: number;
  [key: string]: unknown;
}

export interface MapSnapshotPayload {
  schema_version: string;
  generated_at: string;
  systems: MapSystem[];
  links: MapLink[];
  summary: MapSummary;
}

export interface ReportItem {
  id: string;
  system_name?: string;
  system_id?: number;
  names?: string[];
  source?: string;
  seen_at?: string;
  raw_text?: string;
  observation_id?: string;
  [key: string]: unknown;
}

export interface ObservationItem {
  id: string;
  system_name?: string;
  system_id?: number;
  names?: string[];
  source?: string;
  seen_at?: string;
  raw_text?: string;
  [key: string]: unknown;
}

export interface ActiveIntelItem {
  id: string;
  source: string;
  source_instance?: string;
  system_name?: string;
  system_id?: number | null;
  target_type?: string;
  name?: string;
  character_id?: number | null;
  raw_text?: string;
  metadata?: Record<string, unknown>;
  first_seen_at?: string;
  last_seen_at?: string;
  expires_at?: string;
  left_at?: string;
  cleared_at?: string;
  active?: boolean;
  seen_count?: number;
  confidence?: number | null;
  source_observation_ids?: string[];
  [key: string]: unknown;
}

export interface VerifiedCharacter {
  character_id: number;
  name: string;
}

export interface AlertItem {
  id: string;
  system_name?: string;
  system_id?: number;
  names?: string[];
  character_ids?: number[];
  verified_characters?: VerifiedCharacter[];
  level?: Level;
  score?: number;
  created_at?: string;
  acknowledged?: boolean;
  [key: string]: unknown;
}

export interface HeartbeatSummary {
  count: number;
  online_count: number;
  stale_count: number;
  by_type?: Record<string, number>;
  by_status?: Record<string, number>;
  [key: string]: unknown;
}

export interface ClientsPayload {
  count: number;
  heartbeats: Array<Record<string, unknown>>;
  summary: HeartbeatSummary;
}

export interface ConfigPayload {
  schema_version: string;
  whitelist?: string[];
  blacklist?: string[];
  hostile_corporation_ids?: number[];
  hostile_alliance_ids?: number[];
  hostile_standing_threshold?: number | null;
  cooldown_seconds?: number;
  [key: string]: unknown;
}

export interface EsiPayload {
  enabled: boolean;
  authenticated: boolean;
  session?: boolean;
  character_id?: number;
  config?: {
    client_id_configured?: boolean;
    token_file_present?: boolean;
    token_storage?: string;
    scopes?: string[];
    [key: string]: unknown;
  };
  error?: string;
  [key: string]: unknown;
}

export interface EsiLoginPayload {
  status: string;
  authorization_url?: string;
  started_at?: number;
  expires_at?: number;
  timeout_seconds?: number;
  character_id?: number | null;
  error?: string;
  [key: string]: unknown;
}

export interface BootstrapPayload {
  schema_version: string;
  generated_at: string;
  map: MapSnapshotPayload;
  reports: ReportItem[];
  observations?: ObservationItem[];
  active_intel?: ActiveIntelItem[];
  alerts: AlertItem[];
  clients: ClientsPayload;
  config: ConfigPayload | null;
  esi: EsiPayload;
}

export interface PilotObservation {
  id: string;
  pilotName: string;
  systemName?: string;
  systemId?: number;
  systemIds: number[];
  sources: string[];
  level: Level | "unknown";
  score?: number;
  latestSeen?: string;
  evidenceCount: number;
  repeatCount?: number;
}

export interface WorkbenchSummary {
  systems: number;
  hostiles: number;
  reports: number;
  alerts: number;
  onlineClients: number;
}
