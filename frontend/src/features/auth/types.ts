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
  details?: ClientHeartbeatDetails;
}

export interface ClientsSnapshot {
  count: number;
  heartbeats: ClientHeartbeatRecord[];
  summary?: Record<string, unknown>;
}
