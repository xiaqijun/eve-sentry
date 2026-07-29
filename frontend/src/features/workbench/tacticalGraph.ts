import type { AlertItem, BootstrapPayload, Level } from "./types";

export interface TacticalGraphNode {
  id: string;
  name: string;
  kind?: "system" | "hostile-card";
  systemId?: number;
  x: number;
  y: number;
  fx: number;
  fy: number;
  security: number | null;
  hostileCount: number;
  reportCount: number;
  observationCount: number;
  channelIntelCount: number;
  killCount: number | null;
  monitorCount: number;
  monitorOnlineCount: number;
  monitorLabels: string[];
  hasAlerts: boolean;
  isSelected: boolean;
  threatLevel: Level | "unknown";
  threatScore: number | null;
  hostileIntel?: TacticalHostileIntel;
  hostileAnchorX?: number;
  hostileAnchorY?: number;
}

export interface TacticalGraphLink {
  source: string;
  target: string;
  kind?: "gate" | "hostile-intel";
}

export interface TacticalGraphData {
  nodes: TacticalGraphNode[];
  links: TacticalGraphLink[];
}

export interface TacticalHostileIntel {
  characterId: number | null;
  name: string;
  corporation: string;
  alliance: string;
  threatLevel: Level | "unknown";
  threatScore: number | null;
}

interface TacticalGraphOptions {
  includeHostileCards?: boolean;
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

interface ActiveIntelSummary {
  ocrCount: number;
  channelCount: number;
  reportCount: number;
}

const OCR_SOURCES = new Set([
  "local_ocr",
  "local_ocr_seen",
  "ocr",
  "eve-sentry-detector",
]);
const CHANNEL_SOURCES = new Set([
  "channel",
  "intel_channel",
  "intel_channel_report",
]);

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
  for (const heartbeat of bootstrap.clients?.heartbeats || []) {
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

function activeIntelWeight(item: Record<string, unknown>): number {
  const metadata = asRecord(item.metadata);
  const hostileCount = firstNumber(metadata.hostile_count);
  if (hostileCount !== null && hostileCount > 0) {
    return hostileCount;
  }
  const seenCount = firstNumber(item.seen_count);
  if (seenCount !== null && seenCount > 0) {
    return seenCount;
  }
  return 1;
}

function activeIntelIsHostile(item: Record<string, unknown>): boolean {
  const metadata = asRecord(item.metadata);
  const hostileCount = firstNumber(metadata.hostile_count);
  if (hostileCount !== null && hostileCount > 0) {
    return true;
  }

  const source = String(item.source || "").trim().toLowerCase();
  if (CHANNEL_SOURCES.has(source)) {
    return true;
  }
  if (!OCR_SOURCES.has(source)) {
    return false;
  }

  const standing = firstNumber(metadata.contact_standing, metadata.standing);
  if (standing === null) {
    return false;
  }
  if (standing >= 5) {
    return false;
  }
  return standing <= 0;
}

function activeIntelNodeName(
  item: Record<string, unknown>,
  systemsById: Map<number, string>,
  systemsByName: Map<string, string>,
): string {
  const systemId = firstNumber(item.system_id);
  const systemName = firstString(item.system_name);
  return (
    (systemId !== null ? systemsById.get(systemId) : undefined) ||
    systemsByName.get(systemName.toLowerCase()) ||
    ""
  );
}

function summarizeActiveIntel(
  bootstrap: BootstrapPayload,
): Map<string, ActiveIntelSummary> {
  const systemsById = new Map<number, string>();
  const systemsByName = new Map<string, string>();
  for (const system of bootstrap.map.systems) {
    if (typeof system.system_id === "number") {
      systemsById.set(system.system_id, system.name);
    }
    systemsByName.set(system.name.trim().toLowerCase(), system.name);
  }

  const summaries = new Map<string, ActiveIntelSummary>();
  for (const item of bootstrap.active_intel || []) {
    if (item.active === false) {
      continue;
    }
    const source = String(item.source || "").trim().toLowerCase();
    const nodeName = activeIntelNodeName(item, systemsById, systemsByName);
    if (!nodeName) {
      continue;
    }
    const summary = summaries.get(nodeName) || {
      ocrCount: 0,
      channelCount: 0,
      reportCount: 0,
    };
    if (!activeIntelIsHostile(item)) {
      summaries.set(nodeName, summary);
      continue;
    }
    if (OCR_SOURCES.has(source)) {
      summary.ocrCount += 1;
      summary.reportCount += activeIntelWeight(item);
    } else if (CHANNEL_SOURCES.has(source)) {
      summary.channelCount += activeIntelWeight(item);
      summary.reportCount += activeIntelWeight(item);
    }
    summaries.set(nodeName, summary);
  }
  return summaries;
}

function normalizeLevel(value: unknown): Level | "unknown" {
  return value === "low" ||
    value === "medium" ||
    value === "high" ||
    value === "critical"
    ? value
    : "unknown";
}

function systemLookups(bootstrap: BootstrapPayload): {
  byId: Map<number, string>;
  byName: Map<string, string>;
} {
  const byId = new Map<number, string>();
  const byName = new Map<string, string>();
  bootstrap.map.systems.forEach((system) => {
    if (typeof system.system_id === "number") {
      byId.set(system.system_id, system.name);
    }
    byName.set(system.name.trim().toLowerCase(), system.name);
  });
  return { byId, byName };
}

function hostileIntelKey(characterId: number | null, name: string): string {
  return characterId !== null
    ? `id:${characterId}`
    : `name:${name.trim().toLowerCase()}`;
}

function mergeHostileIntel(
  summaries: Map<string, Map<string, TacticalHostileIntel>>,
  systemName: string,
  candidate: Record<string, unknown>,
  threatLevel: Level | "unknown" = "unknown",
  threatScore: number | null = null,
): void {
  const characterId = firstNumber(candidate.character_id, candidate.characterId);
  const name = firstString(candidate.name, candidate.character_name);
  if (characterId === null && !name) {
    return;
  }

  const displayName = name || `角色 ${characterId}`;
  const key = hostileIntelKey(characterId, displayName);
  const systemSummaries =
    summaries.get(systemName) || new Map<string, TacticalHostileIntel>();
  const current = systemSummaries.get(key);
  const nextLevel =
    current && LEVEL_RANK[current.threatLevel] > LEVEL_RANK[threatLevel]
      ? current.threatLevel
      : threatLevel;
  const nextScore =
    current?.threatScore !== null && current?.threatScore !== undefined
      ? Math.max(current.threatScore, threatScore ?? current.threatScore)
      : threatScore;

  systemSummaries.set(key, {
    characterId: characterId ?? current?.characterId ?? null,
    name: firstString(name, current?.name) || displayName,
    corporation:
      firstString(candidate.corporation_name, current?.corporation) || "未知军团",
    alliance:
      firstString(candidate.alliance_name, current?.alliance) || "未知联盟",
    threatLevel: nextLevel,
    threatScore: nextScore,
  });
  summaries.set(systemName, systemSummaries);
}

function summarizeHostileIntel(
  bootstrap: BootstrapPayload,
): Map<string, TacticalHostileIntel[]> {
  const { byId, byName } = systemLookups(bootstrap);
  const summaries = new Map<string, Map<string, TacticalHostileIntel>>();

  for (const item of bootstrap.active_intel || []) {
    if (item.active === false || !activeIntelIsHostile(item)) {
      continue;
    }
    const systemName = activeIntelNodeName(item, byId, byName);
    if (!systemName) {
      continue;
    }
    const metadata = asRecord(item.metadata);
    const profiles = Array.isArray(metadata.character_profiles)
      ? metadata.character_profiles.map(asRecord)
      : [];
    if (profiles.length > 0) {
      profiles.forEach((profile) => mergeHostileIntel(summaries, systemName, profile));
      continue;
    }
    mergeHostileIntel(summaries, systemName, {
      ...metadata,
      character_id: item.character_id ?? metadata.character_id,
      name: item.name || metadata.name,
    });
  }

  for (const alert of bootstrap.alerts) {
    if (alert.classification !== "red") {
      continue;
    }
    const systemName =
      (typeof alert.system_id === "number" ? byId.get(alert.system_id) : undefined) ||
      byName.get(String(alert.system_name || "").trim().toLowerCase());
    if (!systemName) {
      continue;
    }
    const alertRecord = asRecord(alert);
    for (const character of alert.verified_characters || []) {
      mergeHostileIntel(
        summaries,
        systemName,
        {
          ...alertRecord,
          character_id: character.character_id,
          name: character.name,
        },
        normalizeLevel(alert.level),
        typeof alert.score === "number" ? alert.score : null,
      );
    }
  }

  const result = new Map<string, TacticalHostileIntel[]>();
  summaries.forEach((items, systemName) => {
    result.set(
      systemName,
      [...items.values()].sort(
        (left, right) =>
          LEVEL_RANK[right.threatLevel] - LEVEL_RANK[left.threatLevel] ||
          (right.threatScore ?? -1) - (left.threatScore ?? -1) ||
          left.name.localeCompare(right.name),
      ),
    );
  });
  return result;
}

export function buildTacticalGraph(
  bootstrap: BootstrapPayload,
  selectedSystemId?: number | null,
  options: TacticalGraphOptions = {},
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
  const activeIntelBySystem = summarizeActiveIntel(bootstrap);
  const hasActiveIntelPayload = Array.isArray(bootstrap.active_intel);

  const systemNodes = bootstrap.map.systems.map((system) => {
      const systemAlerts =
        typeof system.system_id === "number"
          ? alertsBySystem.get(system.system_id) || []
          : [];
      const alertSummary = summarizeAlerts(systemAlerts);
      const x = Number(system.x || 0);
      const y = Number(system.y || 0);
      const activeSummary = activeIntelBySystem.get(system.name) || {
        ocrCount: 0,
        channelCount: 0,
        reportCount: 0,
      };
      const reportCount = hasActiveIntelPayload
        ? activeSummary.reportCount
        : Number(system.report_count || 0);
      const hostileCount = hasActiveIntelPayload
        ? activeSummary.ocrCount
        : Number(system.hostile_count || 0);
      const channelIntelCount = hasActiveIntelPayload
        ? activeSummary.channelCount
        : 0;
      const realtimeSignalCount = hasActiveIntelPayload
        ? hostileCount + channelIntelCount
        : Math.max(reportCount, hostileCount);
      const hasRealtimeIntel = hostileCount > 0;
      const monitorSummary = monitorsBySystem.get(system.name) || {
        count: 0,
        onlineCount: 0,
        labels: [],
      };
      return {
        id: system.name,
        name: system.name,
        kind: "system" as const,
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
        channelIntelCount,
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
    });
  const gateLinks: TacticalGraphLink[] = bootstrap.map.links.map((link) => ({
      source: link.from,
      target: link.to,
    }));

  if (!options.includeHostileCards) {
    return { links: gateLinks, nodes: systemNodes };
  }

  const hostileIntelBySystem = summarizeHostileIntel(bootstrap);
  const centerX = systemNodes.length > 0
    ? systemNodes.reduce((sum, node) => sum + node.x, 0) / systemNodes.length
    : 0;
  const hostileNodes: TacticalGraphNode[] = [];
  const hostileLinks: TacticalGraphLink[] = [];

  systemNodes.forEach((systemNode) => {
    if (systemNode.hostileCount <= 0) {
      return;
    }
    const hostiles = hostileIntelBySystem.get(systemNode.name) || [];
    const direction = systemNode.x >= centerX ? 1 : -1;
    hostiles.forEach((hostile, index) => {
      const verticalOffset = (index - (hostiles.length - 1) / 2) * 86;
      const hostileId = hostileIntelKey(hostile.characterId, hostile.name);
      const nodeId = `hostile:${systemNode.id}:${hostileId}`;
      const x = systemNode.x + direction * 124;
      const y = systemNode.y + verticalOffset;
      hostileNodes.push({
        id: nodeId,
        name: hostile.name,
        kind: "hostile-card",
        systemId: systemNode.systemId,
        x,
        y,
        fx: x,
        fy: y,
        security: null,
        hostileCount: 0,
        reportCount: 0,
        observationCount: 0,
        channelIntelCount: 0,
        killCount: 0,
        monitorCount: 0,
        monitorOnlineCount: 0,
        monitorLabels: [],
        hasAlerts: true,
        isSelected: systemNode.isSelected,
        threatLevel: hostile.threatLevel,
        threatScore: hostile.threatScore,
        hostileIntel: hostile,
        hostileAnchorX: systemNode.x,
        hostileAnchorY: systemNode.y,
      });
      hostileLinks.push({
        source: systemNode.id,
        target: nodeId,
        kind: "hostile-intel",
      });
    });
  });

  return {
    nodes: [...systemNodes, ...hostileNodes],
    links: [...gateLinks, ...hostileLinks],
  };
}
