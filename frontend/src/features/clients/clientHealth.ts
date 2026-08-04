export type ClientHealthState =
  | "healthy"
  | "warning"
  | "offline_recent"
  | "stopped"
  | "offline_history";

export interface ClientHealth {
  state: ClientHealthState;
  isException: boolean;
  isRelevant: boolean;
}

interface ClientHeartbeatLike {
  age_seconds?: unknown;
  details?: unknown;
  online?: unknown;
  seen_at?: unknown;
  stale?: unknown;
  status?: unknown;
}

const ERROR_STATUSES = new Set(["error", "failed", "failure", "exception", "stale"]);

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function hasRuntimeError(details: Record<string, unknown>): boolean {
  if (String(details.last_error || "").trim()) return true;
  if (!Array.isArray(details.targets)) return false;
  return details.targets.some((target) => {
    const value = record(target);
    const runtimeStatus = String(value.runtime_status || "").trim().toLowerCase();
    return Boolean(String(value.last_error || "").trim()) || ERROR_STATUSES.has(runtimeStatus);
  });
}

export function deriveClientHealth(
  client: ClientHeartbeatLike,
  now = Date.now(),
): ClientHealth {
  const details = record(client.details);
  const status = String(client.status || details.status || "").trim().toLowerCase();
  if (status === "stopped") {
    return { state: "stopped", isException: false, isRelevant: false };
  }

  if (client.online === false) {
    const ageSeconds = Number(client.age_seconds);
    const seenAt = new Date(String(client.seen_at || "")).getTime();
    const recent = (Number.isFinite(ageSeconds) && ageSeconds <= 24 * 60 * 60)
      || (!Number.isFinite(ageSeconds) && Number.isFinite(seenAt)
        && seenAt >= now - 24 * 60 * 60 * 1000);
    return recent
      ? { state: "offline_recent", isException: true, isRelevant: true }
      : { state: "offline_history", isException: false, isRelevant: false };
  }

  if (client.stale === true || ERROR_STATUSES.has(status) || hasRuntimeError(details)) {
    return { state: "warning", isException: true, isRelevant: true };
  }
  return { state: "healthy", isException: false, isRelevant: true };
}
