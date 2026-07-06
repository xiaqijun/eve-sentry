import type {
  AlertItem,
  BootstrapPayload,
  ClientsPayload,
  ConfigPayload,
  EsiLoginPayload,
  MapSnapshotPayload,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(
  /\/$/,
  "",
) || "";

function apiPath(path: string): string {
  return `${API_BASE}${path}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiPath(path), {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const payload = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

export async function fetchBootstrap(): Promise<BootstrapPayload> {
  const payload = await request<{ bootstrap: BootstrapPayload }>("/api/v1/bootstrap");
  return payload.bootstrap;
}

export async function fetchMap(): Promise<MapSnapshotPayload> {
  const payload = await request<{ map: MapSnapshotPayload }>("/api/v1/map");
  return payload.map;
}

export async function fetchClients(): Promise<ClientsPayload> {
  const payload = await request<{ clients: ClientsPayload }>("/api/v1/clients");
  return payload.clients;
}

export async function fetchConfig(): Promise<ConfigPayload> {
  const payload = await request<{ config: ConfigPayload }>("/api/v1/config");
  return payload.config;
}

export async function saveConfig(config: ConfigPayload): Promise<ConfigPayload> {
  const payload = await request<{ config: ConfigPayload }>("/api/v1/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
  return payload.config;
}

export async function submitObservation(
  observation: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return request("/api/v1/observations", {
    method: "POST",
    body: JSON.stringify(observation),
  });
}

export async function startEsiLogin(): Promise<EsiLoginPayload> {
  const payload = await request<{ login: EsiLoginPayload }>("/api/v1/esi/login", {
    method: "POST",
  });
  return payload.login;
}

export async function fetchEsiLoginStatus(): Promise<EsiLoginPayload> {
  const payload = await request<{ login: EsiLoginPayload }>("/api/v1/esi/login");
  return payload.login;
}

export function connectAlerts(
  onAlert: (alert: AlertItem) => void,
  since?: string,
  onError?: () => void,
): EventSource {
  const query = new URLSearchParams({
    limit: "50",
    timeout: "30",
  });
  if (since) {
    query.set("since", since);
  }
  const stream = new EventSource(apiPath(`/api/v1/events?${query.toString()}`));
  stream.addEventListener("alert", (event) => {
    onAlert(JSON.parse(event.data) as AlertItem);
  });
  stream.addEventListener("error", () => {
    onError?.();
  });
  return stream;
}
