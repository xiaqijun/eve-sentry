export type UserRole = "admin" | "member";

export interface AuthUser {
  user_id: string;
  username: string;
  display_name: string;
  role: UserRole;
  status: "active" | "disabled";
  must_change_password: boolean;
  disabled_reason?: string;
  created_at?: string;
  updated_at?: string;
}

export interface SecuritySettings {
  key_risk_control: boolean;
}

export interface ApiKeyRecord {
  key_id: string;
  user_id: string;
  name: string;
  key_prefix: string;
  key_type: "desktop" | "service_readonly";
  status: "active" | "revoked";
  identity_verified: boolean;
  created_at: string;
  last_used_at?: string;
  revoked_at?: string;
  revoked_reason?: string;
  secret?: string;
}

export interface WhitelistCharacter {
  user_id: string;
  character_id: number;
  character_name: string;
  note?: string;
  created_at: string;
}

export interface VerifiedCharacter {
  user_id: string;
  character_id: number;
  character_name: string;
  corporation_id?: number | null;
  corporation_name?: string;
  first_seen_at: string;
  last_seen_at: string;
}

export interface AdminUser extends AuthUser {
  keys: ApiKeyRecord[];
  whitelist: WhitelistCharacter[];
  verified_characters: VerifiedCharacter[];
}

export interface AllowedCorporation {
  corporation_id: number;
  corporation_name: string;
  created_at: string;
}

export interface AuditRecord {
  audit_id: string;
  actor_user_id?: string;
  target_user_id?: string;
  action: string;
  details?: Record<string, unknown> | string;
  created_at: string;
}

export interface ClientHeartbeatDetails {
  client_version?: string;
  host?: string;
  last_action?: string;
  last_error?: string;
  [key: string]: unknown;
}

export interface ClientHeartbeatRecord {
  client_id: string;
  client_type?: string;
  label?: string;
  status?: string;
  seen_at: string;
  online?: boolean;
  age_seconds?: number;
  details?: ClientHeartbeatDetails;
}

export interface AdminClientOwner {
  user_id: string;
  username: string;
  display_name: string;
  role: UserRole;
  status: "active" | "disabled";
}

export interface AdminClientHeartbeatRecord extends ClientHeartbeatRecord {
  owner?: AdminClientOwner | null;
  key?: ApiKeyRecord | null;
  user_id?: string;
  api_key_id?: string;
  remote_ip?: string;
}

export interface ClientsSnapshot {
  count: number;
  heartbeats: ClientHeartbeatRecord[];
  summary?: Record<string, unknown>;
}

export interface AdminClientKeyUsage {
  key: ApiKeyRecord;
  owner: AdminClientOwner;
  client_count: number;
  linked_clients: AdminClientHeartbeatRecord[];
  online_count: number;
  last_client: AdminClientHeartbeatRecord | null;
  last_ip: string;
}

export interface AdminClientsSnapshot {
  clients: Omit<ClientsSnapshot, "heartbeats"> & {
    heartbeats: AdminClientHeartbeatRecord[];
  };
  keys: AdminClientKeyUsage[];
}

export interface EsiGatewayHealth {
  ok?: boolean;
  service?: string;
  version?: string;
  uptime_seconds?: number;
  requests?: number;
  total_requests?: number;
  upstream_requests?: number;
  cache_misses?: number;
  errors?: number;
  cache_hits?: number;
  cache_entries?: number;
  cache_hit_rate?: number;
  request_rate_per_second?: number;
  upstream_rate_per_second?: number;
  rate_limit_per_second?: number;
  latency_ms?: {
    last?: number;
    average?: number;
  };
  last_error_at?: number | null;
  endpoints?: Record<string, {
    requests?: number;
    cache_hits?: number;
    cache_misses?: number;
    upstream_requests?: number;
    errors?: number;
    cache_hit_rate?: number;
    last_latency_ms?: number;
    average_latency_ms?: number;
  }>;
}

export interface EsiGatewaySnapshot {
  gateway: {
    configured: boolean;
    reachable: boolean;
    url?: string;
    checked_at?: string;
    error?: string;
    health?: EsiGatewayHealth;
  };
  client_metrics: {
    counts?: Record<string, number>;
    totals?: {
      requests?: number;
      remote_requests?: number;
      cache_hits?: number;
      cache_misses?: number;
      fallback_requests?: number;
      request_rate_per_second?: number;
    };
    endpoints?: Record<string, {
      requests?: number;
      remote_requests?: number;
      cache_hits?: number;
      cache_misses?: number;
      fallback_requests?: number;
      errors?: number;
      cache_hit_rate?: number;
      last_ms?: number;
      average_ms?: number;
      p50_ms?: number;
      p95_ms?: number;
    }>;
    durations_ms?: Record<string, {
      count?: number;
      last?: number;
      p50?: number;
      p95?: number;
    }>;
  };
  esi?: Record<string, unknown>;
}
