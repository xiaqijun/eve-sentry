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
    expect(observations[0].sources).toEqual(["预警频道", "本地OCR", "预警"]);
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
    expect(observations[0].sources).toEqual(["本地OCR"]);
  });

  it("uses readable source labels for every realtime intel source family", () => {
    const payload = {
      ...bootstrap,
      active_intel: [
        { id: "active-1", source: "channel", name: "Channel Pilot", active: true },
        { id: "active-2", source: "intel_channel", name: "Intel Pilot", active: true },
        { id: "active-3", source: "intel_channel_report", name: "Report Pilot", active: true },
        {
          id: "active-4",
          source: "local_ocr",
          name: "Local Pilot",
          active: true,
          metadata: { contact_standing: 0 },
        },
        {
          id: "active-5",
          source: "local_ocr_seen",
          name: "Seen Pilot",
          active: true,
          metadata: { contact_standing: 0 },
        },
        {
          id: "active-6",
          source: "ocr",
          name: "Ocr Pilot",
          active: true,
          metadata: { contact_standing: 0 },
        },
        {
          id: "active-7",
          source: "eve-sentry-detector",
          name: "Detector Pilot",
          active: true,
          metadata: { contact_standing: 0 },
        },
        {
          id: "active-8",
          source: "manual",
          name: "Manual Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-9",
          source: "manual_intel",
          name: "Manual Intel Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-10",
          source: "zkill",
          name: "Zkill Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-11",
          source: "zkillboard",
          name: "Zkillboard Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-12",
          source: "killboard",
          name: "Killboard Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-13",
          source: "esi",
          name: "Esi Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-14",
          source: "",
          name: "Unknown Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
        {
          id: "active-15",
          source: "unmapped_feed",
          name: "Mystery Pilot",
          active: true,
          metadata: { hostile_count: 1 },
        },
      ],
    } satisfies BootstrapPayload;

    const sourcesByName = new Map(
      buildPilotObservations(payload).map((item) => [item.pilotName, item.sources[0]]),
    );

    expect(sourcesByName).toMatchObject(
      new Map([
        ["Channel Pilot", "预警频道"],
        ["Intel Pilot", "预警频道"],
        ["Report Pilot", "预警频道"],
        ["Local Pilot", "本地OCR"],
        ["Seen Pilot", "本地OCR"],
        ["Ocr Pilot", "本地OCR"],
        ["Detector Pilot", "本地OCR"],
        ["Manual Pilot", "手动上报"],
        ["Manual Intel Pilot", "手动上报"],
        ["Zkill Pilot", "zKill"],
        ["Zkillboard Pilot", "zKill"],
        ["Killboard Pilot", "zKill"],
        ["Esi Pilot", "ESI"],
        ["Unknown Pilot", "情报"],
        ["Mystery Pilot", "情报"],
      ]),
    );
    expect(sourcesByName.get("Mystery Pilot")).toBe(sourcesByName.get("Unknown Pilot"));
  });

  it("uses a readable fallback when active intel has no name or raw text", () => {
    const payload = {
      ...bootstrap,
      active_intel: [
        {
          id: "active-unknown",
          source: "channel",
          name: "",
          raw_text: "",
          active: true,
          seen_count: 1,
        },
      ],
    } satisfies BootstrapPayload;

    expect(buildPilotObservations(payload)[0].pilotName).toBe("未命名目标");
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
          metadata: { contact_standing: 0 },
        },
      ],
    } satisfies BootstrapPayload;

    const items = buildPilotObservations(payload);

    expect(items).toHaveLength(1);
    expect(items[0].pilotName).toBe("Alice");
    expect(items[0].repeatCount).toBe(3);
  });

  it("keeps detector names visible when hostility is confirmed by a red icon", () => {
    const payload = {
      ...bootstrap,
      reports: [],
      observations: [],
      alerts: [],
      active_intel: [
        {
          id: "active-icon-only",
          source: "eve-sentry-detector",
          system_name: "S-KSWL",
          name: "Obi Augurey",
          active: true,
          metadata: {
            hostile_icon_detected: true,
            hostile_icon_count: 1,
          },
        },
      ],
    } satisfies BootstrapPayload;

    expect(buildPilotObservations(payload).map((item) => item.pilotName)).toEqual([
      "Obi Augurey",
    ]);
  });

  it("does not fall back to stale history when active intel is empty", () => {
    const payload = {
      ...bootstrap,
      active_intel: [],
    } satisfies BootstrapPayload;

    expect(buildPilotObservations(payload)).toEqual([]);
  });

  it("excludes friendly OCR active intel from the hostile pilot list", () => {
    const payload = {
      ...bootstrap,
      reports: [],
      observations: [],
      alerts: [],
      active_intel: [
        {
          id: "friendly-1",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "S-KSWL",
          system_id: 30000001,
          target_type: "character",
          name: "Hajimi6",
          active: true,
          seen_count: 1,
          last_seen_at: "2026-07-03T10:00:04+00:00",
          metadata: {
            contact_standing: 10,
            standing_source: "esi_self",
            standing_contact_type: "character",
          },
        },
        {
          id: "hostile-1",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "S-KSWL",
          system_id: 30000001,
          target_type: "character",
          name: "Neutral Pilot",
          active: true,
          seen_count: 1,
          last_seen_at: "2026-07-03T10:00:05+00:00",
          metadata: { contact_standing: 0 },
        },
      ],
    } satisfies BootstrapPayload;

    const items = buildPilotObservations(payload);

    expect(items).toHaveLength(1);
    expect(items[0].pilotName).toBe("Neutral Pilot");
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
          metadata: { contact_standing: 0 },
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
          metadata: { contact_standing: 0 },
        },
      ],
    } satisfies BootstrapPayload;

    const items = buildPilotObservations(payload, 30000001);

    expect(items).toHaveLength(1);
    expect(items[0].pilotName).toBe("Alice");
  });
});
