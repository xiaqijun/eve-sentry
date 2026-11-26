import type {
  AdminUser,
  AdminClientHeartbeatRecord,
  AdminClientKeyUsage,
  AdminClientsSnapshot,
  ApiKeyRecord,
  AuthUser,
  EsiGatewaySnapshot,
  EsiAuthStatus,
  EsiLoginSnapshot,
  SeatIntegrationSettings,
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

export async function fetchSeatIntegration(): Promise<SeatIntegrationSettings> {
  const payload = await apiRequest<{ settings: SeatIntegrationSettings }>("/api/v1/admin/seat-integration");
  return payload.settings;
}

export async function fetchCurrentSeatServiceToken(): Promise<string> {
  const payload = await apiRequest<{ service_token: string }>("/api/v1/admin/seat-integration/copy-token", { method: "POST" });
  if (typeof payload.service_token !== "string" || !payload.service_token) {
    throw new ApiError("无法读取当前令牌", 502, "invalid_seat_token_response");
  }
  return payload.service_token;
}

type SeatIntegrationEdit = { revision: number } & (
  { enabled: boolean } | { service_token: string } | { generate_service_token: true }
);

function writeSeatIntegration(edit: SeatIntegrationEdit) {
  return apiRequest<{ settings: SeatIntegrationSettings; service_token?: string }>("/api/v1/admin/seat-integration", {
    method: "POST",
    body: JSON.stringify(edit),
  });
}

export async function updateSeatIntegration(enabled: boolean, revision: number): Promise<SeatIntegrationSettings> {
  return (await writeSeatIntegration({ enabled, revision })).settings;
}

export function saveSeatServiceToken(revision: number, token?: string) {
  return writeSeatIntegration(token === undefined
    ? { revision, generate_service_token: true }
    : { revision, service_token: token });
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

export async function listMyKeys(): Promise<{ keys: ApiKeyRecord[]; localIssuance: boolean }> {
  const payload = await apiRequest<{ keys?: unknown; local_issuance?: boolean }>("/api/v1/me/keys");
  return {
    keys: arrayOrEmpty<ApiKeyRecord>(payload.keys),
    localIssuance: payload.local_issuance === true,
  };
}

export async function createMyKey(name: string): Promise<ApiKeyRecord & { secret: string }> {
  const payload = await apiRequest<{ key: ApiKeyRecord & { secret: string } }>("/api/v1/me/keys", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  return payload.key;
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

export async function fetchEsiStatus(): Promise<EsiAuthStatus> {
  return apiRequest<EsiAuthStatus>("/api/v1/esi/status");
}

export async function startEsiLogin(): Promise<EsiLoginSnapshot> {
  const payload = await apiRequest<{ login?: EsiLoginSnapshot }>("/api/v1/esi/login", {
    method: "POST",
  });
  return payload.login || { status: "error", error: "服务器没有返回登录状态" };
}

export async function fetchEsiLoginStatus(): Promise<EsiLoginSnapshot> {
  const payload = await apiRequest<{ login?: EsiLoginSnapshot }>("/api/v1/esi/login");
  return payload.login || { status: "error", error: "服务器没有返回登录状态" };
}
