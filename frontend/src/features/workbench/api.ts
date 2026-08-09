import type {
  AlertItem,
  BootstrapPayload,
  ClientsPayload,
  ConfigPayload,
  EsiLoginPayload,
  MapSnapshotPayload,
} from "./types";

type BootstrapStreamPayload = Partial<Omit<BootstrapPayload, "map">> & {
  map?: Partial<MapSnapshotPayload>;
};
import { apiPath, apiRequest } from "../auth/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, init);
}

export async function fetchBootstrap(): Promise<BootstrapPayload> {
  const payload = await request<{ bootstrap?: Partial<BootstrapPayload> }>("/api/v1/bootstrap");
  const bootstrap = payload.bootstrap || {};
  return {
    schema_version: String(bootstrap.schema_version || "intel_bootstrap.v1"),
    generated_at: String(bootstrap.generated_at || new Date().toISOString()),
    map: {
      schema_version: String(bootstrap.map?.schema_version || "map.v1"),
      generated_at: String(bootstrap.map?.generated_at || new Date().toISOString()),
      systems: Array.isArray(bootstrap.map?.systems) ? bootstrap.map.systems : [],
      links: Array.isArray(bootstrap.map?.links) ? bootstrap.map.links : [],
      summary: bootstrap.map?.summary || {},
    },
    reports: Array.isArray(bootstrap.reports) ? bootstrap.reports : [],
    observations: Array.isArray(bootstrap.observations) ? bootstrap.observations : [],
    active_intel: Array.isArray(bootstrap.active_intel) ? bootstrap.active_intel : [],
    alerts: Array.isArray(bootstrap.alerts) ? bootstrap.alerts : [],
    clients: {
      count: Number(bootstrap.clients?.count ?? 0),
      heartbeats: Array.isArray(bootstrap.clients?.heartbeats) ? bootstrap.clients.heartbeats : [],
      summary: bootstrap.clients?.summary || { count: 0, online_count: 0, stale_count: 0 },
    },
    config: bootstrap.config || null,
    esi: bootstrap.esi || { enabled: false, authenticated: false },
  };
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
  onBootstrap?: (bootstrap: BootstrapStreamPayload) => void,
): EventSource {
  const query = new URLSearchParams({
    // The workbench needs active-intel snapshots, not only alert events, so
    // the server can push live star-map changes when a hostile appears.
    bootstrap: "1",
    limit: "50",
    // Keep the stream open for the server-side maximum. Short-lived streams
    // create reconnect gaps in which the map can only fall back to polling.
    timeout: "300",
  });
  if (since) {
    query.set("since", since);
  }
  const stream = new EventSource(
    apiPath(`/api/v1/events?${query.toString()}`),
    { withCredentials: true },
  );
  stream.addEventListener("alert", (event) => {
    try {
      onAlert(JSON.parse(event.data) as AlertItem);
    } catch {
      onError?.();
    }
  });
  stream.addEventListener("bootstrap", (event) => {
    try {
      onBootstrap?.(JSON.parse(event.data) as BootstrapStreamPayload);
    } catch {
      onError?.();
    }
  });
  stream.addEventListener("error", () => {
    onError?.();
  });
  return stream;
}
