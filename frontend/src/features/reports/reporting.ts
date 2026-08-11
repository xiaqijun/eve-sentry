import type {
  AlertItem,
  Level,
  VerifiedCharacter,
  ZkillStats,
} from "../workbench/types";

export type ReportRange = "24h" | "7d" | "30d" | "all";

export interface TrendPoint {
  key: string;
  label: string;
  count: number;
}

export interface SystemReportRow {
  name: string;
  incidentCount: number;
  targetSightings: number;
  uniqueTargets: number;
  lastSeen?: string;
}

export interface TargetReportRow {
  characterId: number;
  name: string;
  incidentCount: number;
  systems: string[];
  lastSeen?: string;
  zkill?: ZkillStats;
  dangerRatio: number | null;
}

export interface WaveReportRow {
  id: string;
  systemName: string;
  peakHostileCount: number;
  incidentCount: number;
  uniqueTargets: number;
  startedAt?: string;
  lastSeen?: string;
  endedAt?: string;
  active: boolean;
  targets: TargetReportRow[];
}

export interface HostileWaveLifecycle {
  id: string;
  system_name?: string;
  system_id?: number | null;
  started_at?: string;
  last_seen_at?: string;
  cleared_at?: string;
  active?: boolean;
  peak_hostile_count?: number;
}

export interface SeverityReportRow {
  level: Level | "unknown";
  count: number;
}

export interface HostileReport {
  sourceCount: number;
  incidentCount: number;
  excludedCount: number;
  verificationRate: number;
  targetSightings: number;
  uniqueTargets: number;
  systemCount: number;
  highRiskCount: number;
  averagePerDay: number;
  peakTargetsPerIncident: number;
  repeatTargetCount: number;
  crossSystemTargetCount: number;
  highRiskRate: number;
  averageTargetsPerIncident: number;
  waveCount: number;
  peakWaveHostiles: number;
  peakWaveTargets: number;
  zkillCoverage: number;
  trend: TrendPoint[];
  systems: SystemReportRow[];
  targets: TargetReportRow[];
  severity: SeverityReportRow[];
  recent: AlertItem[];
  waves: WaveReportRow[];
}

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

function startOfLocalDay(timestamp: number): number {
  const date = new Date(timestamp);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

export function reportRangeStart(
  range: ReportRange,
  nowMs: number = Date.now(),
): number | null {
  if (range === "24h") {
    return nowMs - DAY_MS;
  }
  if (range === "7d") {
    return startOfLocalDay(nowMs) - 6 * DAY_MS;
  }
  if (range === "30d") {
    return startOfLocalDay(nowMs) - 29 * DAY_MS;
  }
  return null;
}

function parsedTime(value?: string): number | null {
  if (!value) {
    return null;
  }
  const timestamp = new Date(value).getTime();
  return Number.isNaN(timestamp) ? null : timestamp;
}

function cleanNames(alert: AlertItem): string[] {
  return [...new Set(
    (alert.names || [])
      .map((name) => String(name).trim())
      .filter(Boolean),
  )];
}

function cleanVerifiedCharacters(alert: AlertItem): VerifiedCharacter[] {
  const seen = new Set<number>();
  const characters: VerifiedCharacter[] = [];
  (alert.verified_characters || []).forEach((item) => {
    const characterId = Number(item?.character_id);
    const name = String(item?.name || "").trim();
    if (!Number.isInteger(characterId) || characterId <= 0 || !name || seen.has(characterId)) {
      return;
    }
    seen.add(characterId);
    const zkill = cleanZkillStats(item?.zkill);
    characters.push({
      character_id: characterId,
      name,
      ...(zkill ? { zkill } : {}),
    });
  });
  return characters;
}

function cleanZkillStats(value: unknown): ZkillStats | undefined {
  if (typeof value !== "object" || value === null) {
    return undefined;
  }
  const input = value as Record<string, unknown>;
  const result: ZkillStats = {};
  const numericFields = [
    "character_id",
    "danger_ratio",
    "gang_ratio",
    "solo_ratio",
    "ships_destroyed",
    "ships_lost",
    "isk_destroyed",
    "isk_lost",
  ] as const;
  numericFields.forEach((key) => {
    const number = Number(input[key]);
    if (Number.isFinite(number) && number >= 0) {
      result[key] = number;
    }
  });
  ["source", "fetched_at", "source_url"].forEach((key) => {
    const text = String(input[key] || "").trim();
    if (text) {
      (result as Record<string, unknown>)[key] = text;
    }
  });
  return Object.keys(result).length > 0 ? result : undefined;
}

function buildWaves(
  alerts: AlertItem[],
  lifecycles: HostileWaveLifecycle[],
  nowMs: number,
): WaveReportRow[] {
  const seenIds = new Set<string>();
  const waves = lifecycles
    .map((item) => {
      const id = String(item.id || "").trim();
      const systemName = String(item.system_name || "未知星系").trim() || "未知星系";
      const startedAt = String(item.started_at || "").trim();
      const startedMs = parsedTime(startedAt);
      const endedAt = String(item.cleared_at || "").trim();
      const endedMs = parsedTime(endedAt);
      const rawPeakHostileCount = Number(item.peak_hostile_count ?? 0);
      const peakHostileCount = Number.isFinite(rawPeakHostileCount)
        ? Math.max(0, Math.floor(rawPeakHostileCount))
        : 0;
      if (
        !id
        || seenIds.has(id)
        || startedMs === null
        || startedMs > nowMs
        || (endedAt && (endedMs === null || endedMs < startedMs))
      ) {
        return null;
      }
      seenIds.add(id);
      return {
        id,
        systemName,
        peakHostileCount,
        incidentCount: 0,
        uniqueTargets: 0,
        startedAt,
        lastSeen: String(item.last_seen_at || startedAt),
        endedAt: endedAt || undefined,
        active: Boolean(item.active) && !endedAt,
        targetIds: new Set<number>(),
        targetMap: new Map<number, TargetReportRow>(),
        startedMs,
        endedMs,
      };
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
    .sort((left, right) => right.startedMs - left.startedMs);

  alerts.forEach((alert) => {
    const timestamp = parsedTime(alert.created_at);
    if (timestamp === null) return;
    const systemKey = cleanSystem(alert).toLocaleLowerCase();
    const wave = waves.find((item) => (
      item.systemName.toLocaleLowerCase() === systemKey
      && timestamp >= item.startedMs
      && (item.endedMs === null || timestamp <= item.endedMs)
    ));
    if (!wave) return;
    wave.incidentCount += 1;
    cleanVerifiedCharacters(alert).forEach((character) => {
      wave.targetIds.add(character.character_id);
      const current = wave.targetMap.get(character.character_id);
      if (current) {
        current.incidentCount += 1;
        current.lastSeen = current.lastSeen || alert.created_at;
        if (!current.systems.includes(wave.systemName)) {
          current.systems.push(wave.systemName);
        }
        const candidateDanger = character.zkill?.danger_ratio;
        const currentDanger = current.zkill?.danger_ratio;
        if (
          character.zkill &&
          (!current.zkill
            || (candidateDanger !== undefined
              && (currentDanger === undefined || candidateDanger > currentDanger)))
        ) {
          current.zkill = character.zkill;
          current.dangerRatio = candidateDanger ?? null;
        }
      } else {
        wave.targetMap.set(character.character_id, {
          characterId: character.character_id,
          name: character.name,
          incidentCount: 1,
          systems: [wave.systemName],
          lastSeen: alert.created_at,
          zkill: character.zkill,
          dangerRatio: character.zkill?.danger_ratio ?? null,
        });
      }
    });
    wave.uniqueTargets = wave.targetIds.size;
  });

  return waves
    .filter((wave) => (
      wave.peakHostileCount > 0
      || (wave.incidentCount > 0 && wave.targetMap.size > 0)
    ))
    .map(({
    targetIds: _targetIds,
    targetMap,
    startedMs: _startedMs,
    endedMs: _endedMs,
    ...wave
  }) => ({
    ...wave,
    targets: [...targetMap.values()].sort((left, right) => (
      (right.dangerRatio ?? -1) - (left.dangerRatio ?? -1)
      || left.name.localeCompare(right.name)
    )),
  }));
}

function verifiedAlert(alert: AlertItem): AlertItem | null {
  if (alert.classification !== "red") {
    return null;
  }
  const characters = cleanVerifiedCharacters(alert);
  if (characters.length === 0) {
    return null;
  }
  return {
    ...alert,
    names: characters.map((item) => item.name),
    character_ids: characters.map((item) => item.character_id),
    verified_characters: characters,
  };
}

function cleanSystem(alert: AlertItem): string {
  return String(alert.system_name || "未知星系").trim() || "未知星系";
}

function localDateKey(timestamp: number): string {
  const date = new Date(timestamp);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localMonthKey(timestamp: number): string {
  const date = new Date(timestamp);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function hourTrend(
  alerts: AlertItem[],
  startMs: number,
  nowMs: number,
): TrendPoint[] {
  const bucketMs = 2 * HOUR_MS;
  const points = Array.from({ length: 12 }, (_, index) => {
    const bucketStart = startMs + index * bucketMs;
    return {
      key: String(bucketStart),
      label: new Date(bucketStart).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }),
      count: 0,
    };
  });
  alerts.forEach((alert) => {
    const timestamp = parsedTime(alert.created_at);
    if (timestamp === null || timestamp < startMs || timestamp > nowMs) {
      return;
    }
    const index = Math.min(11, Math.floor((timestamp - startMs) / bucketMs));
    points[index].count += 1;
  });
  return points;
}

function dayTrend(alerts: AlertItem[], startMs: number, nowMs: number): TrendPoint[] {
  const days = Math.max(1, Math.round((startOfLocalDay(nowMs) - startMs) / DAY_MS) + 1);
  const points = Array.from({ length: days }, (_, index) => {
    const timestamp = startMs + index * DAY_MS;
    const date = new Date(timestamp);
    return {
      key: localDateKey(timestamp),
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      count: 0,
    };
  });
  const byKey = new Map(points.map((point) => [point.key, point]));
  alerts.forEach((alert) => {
    const timestamp = parsedTime(alert.created_at);
    if (timestamp === null) {
      return;
    }
    const point = byKey.get(localDateKey(timestamp));
    if (point) {
      point.count += 1;
    }
  });
  return points;
}

function monthTrend(alerts: AlertItem[], nowMs: number): TrendPoint[] {
  const timestamps = alerts
    .map((alert) => parsedTime(alert.created_at))
    .filter((value): value is number => value !== null);
  if (timestamps.length === 0) {
    return [];
  }
  const earliestTimestamp = timestamps.reduce(
    (earliest, timestamp) => Math.min(earliest, timestamp),
    timestamps[0],
  );
  const first = new Date(earliestTimestamp);
  const last = new Date(nowMs);
  const points: TrendPoint[] = [];
  let year = first.getFullYear();
  let month = first.getMonth();
  while (year < last.getFullYear() || (year === last.getFullYear() && month <= last.getMonth())) {
    const timestamp = new Date(year, month, 1).getTime();
    points.push({
      key: localMonthKey(timestamp),
      label: `${year}/${month + 1}`,
      count: 0,
    });
    month += 1;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  const byKey = new Map(points.map((point) => [point.key, point]));
  timestamps.forEach((timestamp) => {
    const point = byKey.get(localMonthKey(timestamp));
    if (point) {
      point.count += 1;
    }
  });
  return points;
}

function buildTrend(
  alerts: AlertItem[],
  range: ReportRange,
  nowMs: number,
): TrendPoint[] {
  const startMs = reportRangeStart(range, nowMs);
  if (range === "24h" && startMs !== null) {
    return hourTrend(alerts, startMs, nowMs);
  }
  if (startMs !== null) {
    return dayTrend(alerts, startMs, nowMs);
  }
  return monthTrend(alerts, nowMs);
}

export function buildHostileReport(
  sourceAlerts: AlertItem[],
  range: ReportRange,
  nowMs: number = Date.now(),
  sourceWaves: HostileWaveLifecycle[] = [],
): HostileReport {
  const startMs = reportRangeStart(range, nowMs);
  const rangedSourceAlerts = sourceAlerts.filter((alert) => {
      if (startMs === null) {
        return true;
      }
      const timestamp = parsedTime(alert.created_at);
      return timestamp !== null && timestamp >= startMs && timestamp <= nowMs;
    });
  const alerts = rangedSourceAlerts
    .map(verifiedAlert)
    .filter((alert): alert is AlertItem => alert !== null)
    .sort((left, right) => (
      (parsedTime(right.created_at) || 0) - (parsedTime(left.created_at) || 0)
    ));

  const uniqueTargets = new Set<number>();
  const systemMap = new Map<string, {
    incidents: number;
    sightings: number;
    targets: Set<number>;
    lastSeen?: string;
  }>();
  const targetMap = new Map<number, {
    name: string;
    incidents: number;
    systems: Set<string>;
    lastSeen?: string;
    zkill?: ZkillStats;
  }>();
  const severityCounts: Record<Level | "unknown", number> = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    unknown: 0,
  };
  let targetSightings = 0;

  alerts.forEach((alert) => {
    const characters = cleanVerifiedCharacters(alert);
    const names = cleanNames(alert);
    const system = cleanSystem(alert);
    const level = ["critical", "high", "medium", "low"].includes(String(alert.level))
      ? alert.level as Level
      : "unknown";
    severityCounts[level] += 1;
    targetSightings += names.length;

    const systemStats = systemMap.get(system) || {
      incidents: 0,
      sightings: 0,
      targets: new Set<number>(),
      lastSeen: undefined,
    };
    systemStats.incidents += 1;
    systemStats.sightings += names.length;
    if (!systemStats.lastSeen) {
      systemStats.lastSeen = alert.created_at;
    }
    characters.forEach((character) => {
      uniqueTargets.add(character.character_id);
      systemStats.targets.add(character.character_id);
      const targetStats = targetMap.get(character.character_id) || {
        name: character.name,
        incidents: 0,
        systems: new Set<string>(),
        lastSeen: undefined,
        zkill: undefined,
      };
      targetStats.incidents += 1;
      targetStats.systems.add(system);
      if (!targetStats.lastSeen) {
        targetStats.lastSeen = alert.created_at;
      }
      const candidateDanger = character.zkill?.danger_ratio;
      const currentDanger = targetStats.zkill?.danger_ratio;
      if (
        character.zkill &&
        (targetStats.zkill === undefined ||
          (candidateDanger !== undefined && (currentDanger === undefined || candidateDanger > currentDanger)))
      ) {
        targetStats.zkill = character.zkill;
      }
      targetMap.set(character.character_id, targetStats);
    });
    systemMap.set(system, systemStats);
  });

  const systems = [...systemMap.entries()]
    .map(([name, item]) => ({
      name,
      incidentCount: item.incidents,
      targetSightings: item.sightings,
      uniqueTargets: item.targets.size,
      lastSeen: item.lastSeen,
    }))
    .sort((left, right) => (
      right.incidentCount - left.incidentCount
      || right.targetSightings - left.targetSightings
      || left.name.localeCompare(right.name)
    ));
  const targets = [...targetMap.entries()]
    .map(([characterId, item]) => ({
      characterId,
      name: item.name,
      incidentCount: item.incidents,
      systems: [...item.systems].sort(),
      lastSeen: item.lastSeen,
      zkill: item.zkill,
      dangerRatio: item.zkill?.danger_ratio ?? null,
    }))
    .sort((left, right) => (
      right.incidentCount - left.incidentCount
      || (right.dangerRatio ?? -1) - (left.dangerRatio ?? -1)
      || left.name.localeCompare(right.name)
    ));
  const earliestAlertMs = alerts.reduce((earliest, alert) => {
    const timestamp = parsedTime(alert.created_at);
    return timestamp === null ? earliest : Math.min(earliest, timestamp);
  }, nowMs);
  const durationDays = startMs === null
    ? Math.max(1, ((nowMs - earliestAlertMs) / DAY_MS) + 1)
    : Math.max(1, (nowMs - startMs) / DAY_MS);
  const highRiskCount = severityCounts.critical + severityCounts.high;
  const peakTargetsPerIncident = alerts.reduce(
    (maximum, alert) => Math.max(maximum, cleanVerifiedCharacters(alert).length),
    0,
  );
  const repeatTargetCount = [...targetMap.values()]
    .filter((item) => item.incidents > 1).length;
  const crossSystemTargetCount = [...targetMap.values()]
    .filter((item) => item.systems.size > 1).length;
  const rangedWaves = sourceWaves.filter((wave) => {
    const startedAt = parsedTime(wave.started_at);
    if (startedAt === null || startedAt > nowMs) {
      return false;
    }
    if (startMs === null) {
      return true;
    }
    const endedAt = parsedTime(wave.cleared_at || wave.last_seen_at);
    return endedAt === null || endedAt >= startMs;
  });
  const waves = buildWaves(alerts, rangedWaves, nowMs);
  const targetsWithZkill = [...targetMap.values()].filter(
    (item) => item.zkill?.danger_ratio !== undefined,
  ).length;

  return {
    sourceCount: rangedSourceAlerts.length,
    incidentCount: alerts.length,
    excludedCount: rangedSourceAlerts.length - alerts.length,
    verificationRate: rangedSourceAlerts.length > 0
      ? (alerts.length / rangedSourceAlerts.length) * 100
      : 0,
    targetSightings,
    uniqueTargets: uniqueTargets.size,
    systemCount: systemMap.size,
    highRiskCount,
    averagePerDay: alerts.length / durationDays,
    peakTargetsPerIncident,
    repeatTargetCount,
    crossSystemTargetCount,
    highRiskRate: alerts.length > 0 ? (highRiskCount / alerts.length) * 100 : 0,
    averageTargetsPerIncident: alerts.length > 0 ? targetSightings / alerts.length : 0,
    waveCount: waves.length,
    peakWaveHostiles: waves.reduce(
      (maximum, wave) => Math.max(maximum, wave.peakHostileCount),
      0,
    ),
    peakWaveTargets: waves.reduce(
      (maximum, wave) => Math.max(maximum, wave.uniqueTargets),
      0,
    ),
    zkillCoverage: targetMap.size > 0 ? (targetsWithZkill / targetMap.size) * 100 : 0,
    trend: buildTrend(alerts, range, nowMs),
    systems,
    targets,
    severity: (["critical", "high", "medium", "low", "unknown"] as const)
      .map((level) => ({ level, count: severityCounts[level] })),
    // Keep the bounded server response intact here.  The page uses Arco's
    // table pagination and keyword filtering for historical queries.
    recent: alerts,
    waves,
  };
}
