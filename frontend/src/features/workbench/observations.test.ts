import { describe, expect, it } from "vitest";

import type { BootstrapPayload } from "./types";
import { buildPilotObservations } from "./observations";

const bootstrap: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1",
  generated_at: "2026-07-02T12:05:00Z",
  map: {
    schema_version: "map_snapshot.v1",
    generated_at: "2026-07-02T12:05:00Z",
    systems: [
      {
        name: "0-UVHJ",
        system_id: 30003615,
        x: 100,
        y: 120,
      },
    ],
    links: [],
    summary: {},
  },
  reports: [
    {
      id: "report-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      source: "channel",
      seen_at: "2026-07-02T12:01:00Z",
    },
  ],
  observations: [
    {
      id: "observation-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      source: "local_ocr",
      seen_at: "2026-07-02T12:04:00Z",
    },
  ],
  alerts: [
    {
      id: "alert-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      level: "high",
      score: 80,
      created_at: "2026-07-02T12:03:00Z",
    },
  ],
  clients: {
    count: 0,
    heartbeats: [],
    summary: {
      count: 0,
      online_count: 0,
      stale_count: 0,
    },
  },
  config: null,
  esi: {
    enabled: false,
    authenticated: false,
  },
};

describe("buildPilotObservations", () => {
  it("merges the same pilot observed by reports observations and alerts", () => {
    const observations = buildPilotObservations(bootstrap);

    expect(observations).toHaveLength(1);
    expect(observations[0]).toMatchObject({
      pilotName: "Pilot One",
      systemName: "0-UVHJ",
      systemId: 30003615,
      level: "high",
      score: 80,
      evidenceCount: 3,
      latestSeen: "2026-07-02T12:04:00Z",
    });
    expect(observations[0].sources).toEqual(["频道", "OCR", "预警"]);
  });

  it("shows raw observations even when they have no report or alert", () => {
    const observations = buildPilotObservations({
      ...bootstrap,
      reports: [],
      alerts: [],
    });

    expect(observations).toHaveLength(1);
    expect(observations[0]).toMatchObject({
      pilotName: "Pilot One",
      systemName: "0-UVHJ",
      systemId: 30003615,
      level: "unknown",
      latestSeen: "2026-07-02T12:04:00Z",
    });
    expect(observations[0].sources).toEqual(["OCR"]);
  });

  it("filters observations by selected system when provided", () => {
    expect(buildPilotObservations(bootstrap, 30003615)).toHaveLength(1);
    expect(buildPilotObservations(bootstrap, 30000001)).toHaveLength(0);
  });

  it("uses active intel for the realtime hostile pilot list", () => {
    const payload = {
      ...bootstrap,
      reports: [],
      observations: [
        {
          id: "history-1",
          source: "eve-sentry-detector",
          system_name: "S-KSWL",
          names: ["Old Pilot"],
          seen_at: "2026-07-03T09:00:00+00:00",
        },
      ],
      active_intel: [
        {
          id: "active-1",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "S-KSWL",
          system_id: 30000001,
          target_type: "character",
          name: "Alice",
          active: true,
          seen_count: 3,
          last_seen_at: "2026-07-03T10:00:04+00:00",
        },
      ],
    } satisfies BootstrapPayload;

    const items = buildPilotObservations(payload);

    expect(items).toHaveLength(1);
    expect(items[0].pilotName).toBe("Alice");
    expect(items[0].repeatCount).toBe(3);
  });

  it("does not fall back to stale history when active intel is empty", () => {
    const payload = {
      ...bootstrap,
      active_intel: [],
    } satisfies BootstrapPayload;

    expect(buildPilotObservations(payload)).toEqual([]);
  });

  it("filters active intel by selected system when provided", () => {
    const payload = {
      ...bootstrap,
      reports: [],
      observations: [],
      alerts: [],
      active_intel: [
        {
          id: "active-1",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "S-KSWL",
          system_id: 30000001,
          target_type: "character",
          name: "Alice",
          active: true,
          seen_count: 1,
          last_seen_at: "2026-07-03T10:00:04+00:00",
        },
        {
          id: "active-2",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "0-UVHJ",
          system_id: 30003615,
          target_type: "character",
          name: "Bob",
          active: true,
          seen_count: 2,
          last_seen_at: "2026-07-03T10:00:05+00:00",
        },
      ],
    } satisfies BootstrapPayload;

    const items = buildPilotObservations(payload, 30000001);

    expect(items).toHaveLength(1);
    expect(items[0].pilotName).toBe("Alice");
  });
});
