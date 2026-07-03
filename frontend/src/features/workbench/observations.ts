import type {
  ActiveIntelItem,
  AlertItem,
  BootstrapPayload,
  Level,
  ObservationItem,
  PilotObservation,
  ReportItem,
} from "./types";

const LEVEL_RANK: Record<Level | "unknown", number> = {
  unknown: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
};

function normalizeName(value: string): string {
  return value.trim().toLocaleLowerCase();
}

function sourceLabel(source?: string): string {
  switch ((source || "").toLocaleLowerCase()) {
    case "channel":
    case "intel_channel":
    case "intel_channel_report":
      return "频道";
    case "local_ocr":
    case "local_ocr_seen":
    case "ocr":
    case "eve-sentry-detector":
      return "OCR";
    case "manual":
    case "manual_intel":
      return "手动";
    case "zkill":
    case "zkillboard":
    case "killboard":
      return "zKill";
    case "esi":
      return "ESI";
    default:
      return source || "情报";
  }
}

function laterTime(current?: string, next?: string): string | undefined {
  if (!next) {
    return current;
  }
  if (!current) {
    return next;
  }
  const currentTime = Date.parse(current);
  const nextTime = Date.parse(next);
  if (Number.isNaN(currentTime) || Number.isNaN(nextTime)) {
    return next > current ? next : current;
  }
  return nextTime > currentTime ? next : current;
}

function mergeObservation(
  items: Map<string, PilotObservation>,
  input: {
    pilotName: string;
    systemName?: string;
    systemId?: number;
    source: string;
    level?: Level | "unknown";
    score?: number;
    seenAt?: string;
  },
): void {
  const pilotName = input.pilotName.trim();
  if (!pilotName) {
    return;
  }
  const key = normalizeName(pilotName);
  const current = items.get(key);
  if (!current) {
    items.set(key, {
      id: key,
      pilotName,
      systemName: input.systemName,
      systemId: input.systemId,
      systemIds: typeof input.systemId === "number" ? [input.systemId] : [],
      sources: [input.source],
      level: input.level || "unknown",
      score: input.score,
      latestSeen: input.seenAt,
      evidenceCount: 1,
    });
    return;
  }

  if (!current.sources.includes(input.source)) {
    current.sources.push(input.source);
  }
  if (
    typeof input.systemId === "number" &&
    !current.systemIds.includes(input.systemId)
  ) {
    current.systemIds.push(input.systemId);
  }
  if (input.seenAt && laterTime(current.latestSeen, input.seenAt) === input.seenAt) {
    current.systemName = input.systemName || current.systemName;
    current.systemId = input.systemId ?? current.systemId;
  }
  current.latestSeen = laterTime(current.latestSeen, input.seenAt);
  current.evidenceCount += 1;

  const nextLevel = input.level || "unknown";
  if (LEVEL_RANK[nextLevel] > LEVEL_RANK[current.level]) {
    current.level = nextLevel;
  }
  if (
    typeof input.score === "number" &&
    (typeof current.score !== "number" || input.score > current.score)
  ) {
    current.score = input.score;
  }
}

function addReport(
  observations: Map<string, PilotObservation>,
  report: ReportItem,
): void {
  for (const name of report.names || []) {
    mergeObservation(observations, {
      pilotName: name,
      systemName: report.system_name,
      systemId: report.system_id,
      source: sourceLabel(report.source),
      seenAt: report.seen_at,
    });
  }
}

function addObservation(
  observations: Map<string, PilotObservation>,
  observation: ObservationItem,
): void {
  for (const name of observation.names || []) {
    mergeObservation(observations, {
      pilotName: name,
      systemName: observation.system_name,
      systemId: observation.system_id,
      source: sourceLabel(observation.source),
      seenAt: observation.seen_at,
    });
  }
}

function addAlert(
  observations: Map<string, PilotObservation>,
  alert: AlertItem,
): void {
  for (const name of alert.names || []) {
    mergeObservation(observations, {
      pilotName: name,
      systemName: alert.system_name,
      systemId: alert.system_id,
      source: "预警",
      level: alert.level || "unknown",
      score: alert.score,
      seenAt: alert.created_at,
    });
  }
}

function activeIntelObservation(item: ActiveIntelItem): PilotObservation {
  const systemId = typeof item.system_id === "number" ? item.system_id : undefined;
  return {
    id: item.id,
    pilotName: item.name || item.raw_text || "Unknown",
    systemName: item.system_name,
    systemId,
    systemIds: typeof systemId === "number" ? [systemId] : [],
    sources: [sourceLabel(item.source)],
    level: "unknown",
    latestSeen: item.last_seen_at || item.first_seen_at || "",
    evidenceCount: Math.max(1, item.seen_count || 1),
    repeatCount:
      typeof item.seen_count === "number" && item.seen_count > 1
        ? item.seen_count
        : undefined,
  };
}

export function buildPilotObservations(
  bootstrap: BootstrapPayload,
  selectedSystemId?: number | null,
): PilotObservation[] {
  if (Array.isArray(bootstrap.active_intel)) {
    const activeIntel = bootstrap.active_intel.filter((item) => item.active !== false);
    return activeIntel
      .map((item) => activeIntelObservation(item))
      .filter((item) => {
        if (typeof selectedSystemId !== "number") {
          return true;
        }
        return item.systemIds.includes(selectedSystemId);
      })
      .sort((left, right) => {
        return Date.parse(right.latestSeen || "") - Date.parse(left.latestSeen || "");
      });
  }

  const observations = new Map<string, PilotObservation>();
  const reportObservationIds = new Set<string>();
  bootstrap.reports.forEach((report) => {
    if (report.id) {
      reportObservationIds.add(report.id);
    }
    if (typeof report.observation_id === "string" && report.observation_id) {
      reportObservationIds.add(report.observation_id);
    }
    addReport(observations, report);
  });
  (bootstrap.observations || []).forEach((observation) => {
    if (!reportObservationIds.has(observation.id)) {
      addObservation(observations, observation);
    }
  });
  bootstrap.alerts.forEach((alert) => addAlert(observations, alert));

  return Array.from(observations.values())
    .filter((item) => {
      if (typeof selectedSystemId !== "number") {
        return true;
      }
      return item.systemIds.includes(selectedSystemId);
    })
    .sort((left, right) => {
      const levelDelta = LEVEL_RANK[right.level] - LEVEL_RANK[left.level];
      if (levelDelta !== 0) {
        return levelDelta;
      }
      return Date.parse(right.latestSeen || "") - Date.parse(left.latestSeen || "");
    });
}
