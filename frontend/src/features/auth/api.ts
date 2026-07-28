import type {
  AdminUser,
  AllowedCorporation,
  ApiKeyRecord,
  AuditRecord,
  AuthUser,
  ClientsSnapshot,
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
  return (await apiRequest<{ keys: ApiKeyRecord[] }>("/api/v1/me/keys")).keys;
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
  return (await apiRequest<{ users: AdminUser[] }>("/api/v1/admin/users")).users;
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

export async function listCorporations(): Promise<AllowedCorporation[]> {
  return (await apiRequest<{ corporations: AllowedCorporation[] }>(
    "/api/v1/admin/corporations",
  )).corporations;
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
  return (await apiRequest<{ audit: AuditRecord[] }>("/api/v1/admin/audit")).audit;
}

export async function fetchClients(): Promise<ClientsSnapshot> {
  return (await apiRequest<{ clients: ClientsSnapshot }>("/api/v1/clients")).clients;
}
