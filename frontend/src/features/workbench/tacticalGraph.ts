import type { AlertItem, BootstrapPayload, Level } from "./types";

export interface TacticalGraphNode {
  id: string;
  name: string;
  systemId?: number;
  x: number;
  y: number;
  fx: number;
  fy: number;
  security: number | null;
  hostileCount: number;
  reportCount: number;
  observationCount: number;
  killCount: number | null;
  monitorCount: number;
  monitorOnlineCount: number;
  monitorLabels: string[];
  hasAlerts: boolean;
  isSelected: boolean;
  threatLevel: Level | "unknown";
  threatScore: number | null;
}

export interface TacticalGraphLink {
  source: string;
  target: string;
}

export interface TacticalGraphData {
  nodes: TacticalGraphNode[];
  links: TacticalGraphLink[];
}

const LEVEL_RANK: Record<Level | "unknown", number> = {
  unknown: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
};

interface AlertSummary {
  count: number;
  level: Level | "unknown";
  score: number | null;
}

interface MonitorSummary {
  count: number;
  onlineCount: number;
  labels: string[];
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value !== "string") {
      continue;
    }
    const trimmed = value.trim();
    if (trimmed && trimmed.toLowerCase() !== "unknown") {
      return trimmed;
    }
  }
  return "";
}

function firstNumber(...values: unknown[]): number | null {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim()) {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return null;
}

function summarizeAlerts(alerts: AlertItem[]): AlertSummary {
  return alerts.reduce<AlertSummary>(
    (summary, alert) => {
      const level = alert.level || "unknown";
      if (LEVEL_RANK[level] > LEVEL_RANK[summary.level]) {
        summary.level = level;
      }
      if (
        typeof alert.score === "number" &&
        (summary.score === null || alert.score > summary.score)
      ) {
        summary.score = alert.score;
      }
      summary.count += 1;
      return summary;
    },
    {
      count: 0,
      level: "unknown",
      score: null,
    },
  );
}

function summarizeMonitors(
  bootstrap: BootstrapPayload,
): Map<string, MonitorSummary> {
  const systemsById = new Map<number, string>();
  const systemsByName = new Map<string, string>();
  for (const system of bootstrap.map.systems) {
    if (typeof system.system_id === "number") {
      systemsById.set(system.system_id, system.name);
    }
    systemsByName.set(system.name.trim().toLowerCase(), system.name);
  }

  const summaries = new Map<string, MonitorSummary>();
  for (const heartbeat of bootstrap.clients.heartbeats || []) {
    const details = asRecord(heartbeat.details);
    if (heartbeat.online !== true || details.monitoring !== true) {
      continue;
    }
    const systemId = firstNumber(
      heartbeat.system_id,
      heartbeat.solar_system_id,
      details.system_id,
      details.solar_system_id,
    );
    const systemName = firstString(
      heartbeat.system_name,
      heartbeat.system,
      heartbeat.current_system,
      details.system_name,
      details.system,
      details.current_system,
      details.location,
    );
    const nodeName =
      (systemId !== null ? systemsById.get(systemId) : undefined) ||
      systemsByName.get(systemName.toLowerCase());
    if (!nodeName) {
      continue;
    }

    const summary = summaries.get(nodeName) || {
      count: 0,
      onlineCount: 0,
      labels: [],
    };
    const label = firstString(
      heartbeat.label,
      heartbeat.client_type,
      heartbeat.client_id,
    );
    summary.count += 1;
    summary.onlineCount += 1;
    if (label && !summary.labels.includes(label)) {
      summary.labels.push(label);
    }
    summaries.set(nodeName, summary);
  }
  return summaries;
}

export function buildTacticalGraph(
  bootstrap: BootstrapPayload,
  selectedSystemId?: number | null,
): TacticalGraphData {
  const alertsBySystem = new Map<number, AlertItem[]>();
  for (const alert of bootstrap.alerts) {
    if (typeof alert.system_id !== "number") {
      continue;
    }
    alertsBySystem.set(alert.system_id, [
      ...(alertsBySystem.get(alert.system_id) || []),
      alert,
      ]);
  }
  const monitorsBySystem = summarizeMonitors(bootstrap);

  return {
    nodes: bootstrap.map.systems.map((system) => {
      const systemAlerts =
        typeof system.system_id === "number"
          ? alertsBySystem.get(system.system_id) || []
          : [];
      const alertSummary = summarizeAlerts(systemAlerts);
      const x = Number(system.x || 0);
      const y = Number(system.y || 0);
      const reportCount = Number(system.report_count || 0);
      const hostileCount = Number(system.hostile_count || 0);
      const realtimeSignalCount = Math.max(reportCount, hostileCount);
      const hasRealtimeIntel = realtimeSignalCount > 0;
      const monitorSummary = monitorsBySystem.get(system.name) || {
        count: 0,
        onlineCount: 0,
        labels: [],
      };
      return {
        id: system.name,
        name: system.name,
        systemId: system.system_id,
        x,
        y,
        fx: x,
        fy: y,
        security:
          typeof system.security === "number" ? system.security : null,
        hostileCount,
        reportCount,
        observationCount: realtimeSignalCount,
        killCount: firstNumber(system.recent_kill_count) ?? 0,
        monitorCount: monitorSummary.count,
        monitorOnlineCount: monitorSummary.onlineCount,
        monitorLabels: monitorSummary.labels,
        hasAlerts: hasRealtimeIntel,
        isSelected:
          typeof selectedSystemId === "number" &&
          typeof system.system_id === "number" &&
          system.system_id === selectedSystemId,
        threatLevel: hasRealtimeIntel ? alertSummary.level : "unknown",
        threatScore: hasRealtimeIntel ? alertSummary.score : null,
      };
    }),
    links: bootstrap.map.links.map((link) => ({
      source: link.from,
      target: link.to,
    })),
  };
}
