import { describe, expect, it } from "vitest";

import type { AlertItem } from "../workbench/types";
import { buildHostileReport, reportRangeStart } from "./reporting";

const NOW = new Date("2026-07-22T12:00:00Z").getTime();

const alerts: AlertItem[] = [
  {
    id: "alert-1",
    system_name: "S-KSWL",
    names: ["Alice", "Rifter", "Bob"],
    character_ids: [101, 102],
    verified_characters: [
      {
        character_id: 101,
        name: "Alice",
        zkill: { danger_ratio: 82, ships_destroyed: 320, ships_lost: 18 },
      },
      { character_id: 102, name: "Bob" },
    ],
    level: "high",
    classification: "red",
    created_at: "2026-07-22T11:00:00Z",
  },
  {
    id: "alert-2",
    system_name: "1DQ1-A",
    names: ["Alice"],
    character_ids: [101],
    verified_characters: [{ character_id: 101, name: "Alice" }],
    level: "low",
    classification: "red",
    created_at: "2026-07-21T09:00:00Z",
  },
  {
    id: "alert-3",
    system_name: "S-KSWL",
    names: ["Carol"],
    character_ids: [103],
    verified_characters: [{ character_id: 103, name: "Carol" }],
    level: "critical",
    classification: "red",
    created_at: "2026-07-10T09:00:00Z",
  },
  {
    id: "alert-unverified",
    system_name: "S-KSWL",
    names: ["Rifter"],
    character_ids: [999],
    level: "critical",
    classification: "red",
    created_at: "2026-07-22T10:00:00Z",
  },
  {
    id: "alert-friendly",
    system_name: "S-KSWL",
    names: ["Friendly Pilot"],
    character_ids: [104],
    verified_characters: [{ character_id: 104, name: "Friendly Pilot" }],
    level: "low",
    score: 1,
    classification: "white",
    created_at: "2026-07-22T11:30:00Z",
  },
];

describe("hostile reporting", () => {
  it("summarizes incidents, unique targets, systems, risk, and rankings", () => {
    const report = buildHostileReport(alerts, "7d", NOW);

    expect(report.incidentCount).toBe(2);
    expect(report.sourceCount).toBe(4);
    expect(report.excludedCount).toBe(2);
    expect(report.verificationRate).toBe(50);
    expect(report.targetSightings).toBe(3);
    expect(report.uniqueTargets).toBe(2);
    expect(report.systemCount).toBe(2);
    expect(report.highRiskCount).toBe(1);
    expect(report.peakTargetsPerIncident).toBe(2);
    expect(report.repeatTargetCount).toBe(1);
    expect(report.crossSystemTargetCount).toBe(1);
    expect(report.highRiskRate).toBe(50);
    expect(report.averageTargetsPerIncident).toBe(1.5);
    expect(report.waveCount).toBe(2);
    expect(report.peakWaveTargets).toBe(2);
    expect(report.zkillCoverage).toBe(50);
    expect(report.systems.map((item) => item.name)).toEqual(["S-KSWL", "1DQ1-A"]);
    expect(report.targets[0]).toMatchObject({
      name: "Alice",
      incidentCount: 2,
      systems: ["1DQ1-A", "S-KSWL"],
      dangerRatio: 82,
    });
    expect(report.severity.find((item) => item.level === "high")?.count).toBe(1);
    expect(report.severity.find((item) => item.level === "low")?.count).toBe(1);
    expect(report.trend.reduce((sum, item) => sum + item.count, 0)).toBe(2);
    expect(report.recent[0].names).toEqual(["Alice", "Bob"]);
    expect(report.recent.some((item) => item.id === "alert-unverified")).toBe(false);
    expect(report.recent.some((item) => item.id === "alert-friendly")).toBe(false);
  });

  it("includes the complete alert history for the all-time range", () => {
    const report = buildHostileReport(alerts, "all", NOW);

    expect(report.incidentCount).toBe(3);
    expect(report.sourceCount).toBe(5);
    expect(report.excludedCount).toBe(2);
    expect(report.verificationRate).toBe(60);
    expect(report.uniqueTargets).toBe(3);
    expect(report.highRiskCount).toBe(2);
    expect(report.peakTargetsPerIncident).toBe(2);
    expect(report.repeatTargetCount).toBe(1);
    expect(report.crossSystemTargetCount).toBe(1);
    expect(report.trend.reduce((sum, item) => sum + item.count, 0)).toBe(3);
    expect(reportRangeStart("all", NOW)).toBeNull();
  });

  it("returns zero-valued features when no verified hostile records exist", () => {
    const report = buildHostileReport([], "7d", NOW);

    expect(report.peakTargetsPerIncident).toBe(0);
    expect(report.repeatTargetCount).toBe(0);
    expect(report.crossSystemTargetCount).toBe(0);
    expect(report.highRiskRate).toBe(0);
    expect(report.averageTargetsPerIncident).toBe(0);
    expect(report.waveCount).toBe(0);
    expect(report.peakWaveTargets).toBe(0);
    expect(report.zkillCoverage).toBe(0);
    expect(report.sourceCount).toBe(0);
    expect(report.excludedCount).toBe(0);
    expect(report.verificationRate).toBe(0);
  });
});
