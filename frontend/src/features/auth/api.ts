import type {
  AdminUser,
  AdminClientHeartbeatRecord,
  AdminClientKeyUsage,
  AdminClientsSnapshot,
  AllowedCorporation,
  ApiKeyRecord,
  AuditRecord,
  AuthUser,
  ClientsSnapshot,
  ClientHeartbeatRecord,
  SecuritySettings,
  EsiGatewaySnapshot,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "") || "";
let csrfToken = "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
  }
}

export function apiPath(path: string): string {
  return `${API_BASE}${path}`;
}

export function setCsrfToken(value: string): void {
  csrfToken = value;
}

function arrayOrEmpty<T>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function normalizeAdminUser(user: AdminUser): AdminUser {
  return {
    ...user,
    keys: arrayOrEmpty<ApiKeyRecord>(user?.keys),
    whitelist: arrayOrEmpty<AdminUser["whitelist"][number]>(user?.whitelist),
    verified_characters: arrayOrEmpty<AdminUser["verified_characters"][number]>(user?.verified_characters),
  };
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = String(init?.method || "GET").toUpperCase();
  const response = await fetch(apiPath(path), {
    cache: "no-store",
    credentials: "include",
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(csrfToken && !["GET", "HEAD"].includes(method)
        ? { "X-CSRF-Token": csrfToken }
        : {}),
      ...(init?.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({})) as T & {
    error?: string;
    code?: string;
  };
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new Event("eve-sentry-auth-required"));
    }
    throw new ApiError(
      payload.error || "请求失败",
      response.status,
      payload.code || "request_failed",
    );
  }
  return payload;
}

export async function fetchMe(): Promise<AuthUser> {
  const payload = await apiRequest<{ user: AuthUser & { csrf_token?: string } }>(
    "/api/v1/auth/me",
  );
  setCsrfToken(payload.user.csrf_token || "");
  return payload.user;
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const payload = await apiRequest<{ user: AuthUser; csrf_token: string }>(
    "/api/v1/auth/login",
    { method: "POST", body: JSON.stringify({ username, password }) },
  );
  setCsrfToken(payload.csrf_token);
  return payload.user;
}

export async function logout(): Promise<void> {
  await apiRequest("/api/v1/auth/logout", { method: "POST" });
  setCsrfToken("");
}

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await apiRequest("/api/v1/auth/password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

export async function listMyKeys(): Promise<ApiKeyRecord[]> {
  const payload = await apiRequest<{ keys?: unknown }>("/api/v1/me/keys");
  return arrayOrEmpty<ApiKeyRecord>(payload.keys);
}

export async function createMyKey(name: string): Promise<ApiKeyRecord> {
  return (await apiRequest<{ key: ApiKeyRecord }>("/api/v1/me/keys", {
    method: "POST",
    body: JSON.stringify({ name }),
  })).key;
}

export async function revokeKey(keyId: string): Promise<void> {
  await apiRequest(`/api/v1/me/keys/${encodeURIComponent(keyId)}`, { method: "DELETE" });
}

export async function enableKey(keyId: string): Promise<void> {
  await apiRequest(`/api/v1/me/keys/${encodeURIComponent(keyId)}/enable`, {
    method: "POST",
  });
}

export async function deleteKey(keyId: string): Promise<void> {
  await apiRequest(`/api/v1/me/keys/${encodeURIComponent(keyId)}/record`, {
    method: "DELETE",
  });
}

export async function listAdminUsers(): Promise<AdminUser[]> {
  const payload = await apiRequest<{ users?: unknown }>("/api/v1/admin/users");
  return arrayOrEmpty<AdminUser>(payload.users).map(normalizeAdminUser);
}

export async function fetchSecuritySettings(): Promise<SecuritySettings> {
  return (await apiRequest<{ settings: SecuritySettings }>(
    "/api/v1/admin/security-settings",
  )).settings;
}

export async function updateSecuritySettings(
  settings: SecuritySettings,
): Promise<SecuritySettings> {
  return (await apiRequest<{ settings: SecuritySettings }>(
    "/api/v1/admin/security-settings",
    { method: "POST", body: JSON.stringify(settings) },
  )).settings;
}

export async function createUser(input: {
  username: string;
  display_name: string;
  password: string;
  role: "admin" | "member";
}): Promise<void> {
  await apiRequest("/api/v1/admin/users", { method: "POST", body: JSON.stringify(input) });
}

export async function setUserActive(userId: string, active: boolean, reason = ""): Promise<void> {
  await apiRequest(`/api/v1/admin/users/${encodeURIComponent(userId)}/status`, {
    method: "POST",
    body: JSON.stringify({ active, reason }),
  });
}

export async function deleteUser(userId: string): Promise<void> {
  await apiRequest(`/api/v1/admin/users/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}

export async function resetUserPassword(userId: string, password: string): Promise<void> {
  await apiRequest(`/api/v1/admin/users/${encodeURIComponent(userId)}/reset-password`, {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

export async function createServiceKey(userId: string, name: string): Promise<ApiKeyRecord> {
  return (await apiRequest<{ key: ApiKeyRecord }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/service-keys`,
    { method: "POST", body: JSON.stringify({ name }) },
  )).key;
}

export async function createAdminKey(
  userId: string,
  name: string,
  keyType: ApiKeyRecord["key_type"] = "desktop",
): Promise<ApiKeyRecord> {
  return (await apiRequest<{ key: ApiKeyRecord }>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/keys`,
    { method: "POST", body: JSON.stringify({ name, key_type: keyType }) },
  )).key;
}

export async function listCorporations(): Promise<AllowedCorporation[]> {
  const payload = await apiRequest<{ corporations?: unknown }>(
    "/api/v1/admin/corporations",
  );
  return arrayOrEmpty<AllowedCorporation>(payload.corporations);
}

export async function addCorporation(corporationId: number): Promise<void> {
  await apiRequest("/api/v1/admin/corporations", {
    method: "POST",
    body: JSON.stringify({ corporation_id: corporationId }),
  });
}

export async function removeCorporation(corporationId: number): Promise<void> {
  await apiRequest(`/api/v1/admin/corporations/${corporationId}`, { method: "DELETE" });
}

export async function addWhitelistCharacter(
  userId: string,
  characterId: number,
  note: string,
): Promise<void> {
  await apiRequest(`/api/v1/admin/users/${encodeURIComponent(userId)}/characters`, {
    method: "POST",
    body: JSON.stringify({ character_id: characterId, note }),
  });
}

export async function removeWhitelistCharacter(
  userId: string,
  characterId: number,
): Promise<void> {
  await apiRequest(
    `/api/v1/admin/users/${encodeURIComponent(userId)}/characters/${characterId}`,
    { method: "DELETE" },
  );
}

export async function listAudit(): Promise<AuditRecord[]> {
  const payload = await apiRequest<{ audit?: unknown }>("/api/v1/admin/audit");
  return arrayOrEmpty<AuditRecord>(payload.audit);
}

export async function fetchClients(): Promise<ClientsSnapshot> {
  const payload = await apiRequest<{ clients?: Partial<ClientsSnapshot> }>("/api/v1/clients");
  return {
    count: Number(payload.clients?.count ?? 0),
    heartbeats: arrayOrEmpty<ClientHeartbeatRecord>(payload.clients?.heartbeats),
    summary: payload.clients?.summary || {},
  };
}

export async function listAdminClients(): Promise<AdminClientsSnapshot> {
  const payload = await apiRequest<Partial<AdminClientsSnapshot>>("/api/v1/admin/clients");
  const clients: Partial<AdminClientsSnapshot["clients"]> = payload.clients || {};
  const normalizedClients: AdminClientsSnapshot["clients"] = {
    count: Number(clients.count ?? 0),
    heartbeats: arrayOrEmpty<AdminClientHeartbeatRecord>(clients.heartbeats),
    ...(clients.summary ? { summary: clients.summary } : {}),
  };
  return {
    clients: normalizedClients,
    keys: arrayOrEmpty<AdminClientKeyUsage>(payload.keys),
  };
}

export async function fetchEsiGateway(): Promise<EsiGatewaySnapshot> {
  return apiRequest<EsiGatewaySnapshot>("/api/v1/admin/esi-gateway");
}
