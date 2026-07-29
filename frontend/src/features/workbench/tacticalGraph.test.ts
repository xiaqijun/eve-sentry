import { describe, expect, it } from "vitest";

import type { BootstrapPayload } from "./types";
import { buildTacticalGraph } from "./tacticalGraph";

const bootstrap: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1",
  generated_at: "2026-07-02T12:00:00Z",
  map: {
    schema_version: "map_snapshot.v1",
    generated_at: "2026-07-02T12:00:00Z",
    systems: [
      {
        name: "0-UVHJ",
        system_id: 30003615,
        x: 100,
        y: 120,
        hostile_count: 2,
        report_count: 3,
        security: -0.1,
      },
      {
        name: "NCG-PW",
        system_id: 30003616,
        x: 180,
        y: 150,
        hostile_count: 0,
        report_count: 0,
        security: -0.3,
      },
    ],
    links: [{ from: "0-UVHJ", to: "NCG-PW" }],
    summary: {},
  },
  reports: [],
  alerts: [
    {
      id: "alert-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      level: "high",
      score: 80,
      created_at: "2026-07-02T12:01:00Z",
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

describe("buildTacticalGraph", () => {
  it("keeps SDE coordinates fixed for the tactical star map", () => {
    const graph = buildTacticalGraph(bootstrap, 30003615);

    expect(graph.nodes).toHaveLength(2);
    expect(graph.links).toEqual([{ source: "0-UVHJ", target: "NCG-PW" }]);
    expect(graph.nodes[0]).toMatchObject({
      id: "0-UVHJ",
      name: "0-UVHJ",
      systemId: 30003615,
      x: 100,
      y: 120,
      fx: 100,
      fy: 120,
      hostileCount: 2,
      observationCount: 3,
      hasAlerts: true,
      isSelected: true,
      threatLevel: "high",
      threatScore: 80,
    });
  });

  it("does not light map nodes from historical alerts without realtime intel", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: [
          {
            name: "0-UVHJ",
            system_id: 30003615,
            x: 100,
            y: 120,
            hostile_count: 0,
            report_count: 0,
            security: -0.1,
          },
        ],
      },
    });

    expect(graph.nodes[0]).toMatchObject({
      hostileCount: 0,
      observationCount: 0,
      hasAlerts: false,
      threatLevel: "unknown",
      threatScore: null,
    });
  });

  it("keeps realtime heat separate from active report count", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: [
          {
            name: "0-UVHJ",
            system_id: 30003615,
            x: 100,
            y: 120,
            hostile_count: 3,
            report_count: 1,
            security: -0.1,
          },
        ],
      },
    });

    expect(graph.nodes[0]).toMatchObject({
      hostileCount: 3,
      reportCount: 1,
      observationCount: 3,
      hasAlerts: true,
    });
  });

  it("keeps channel intel off the primary hostile HUD when active intel is available", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      active_intel: [
        {
          id: "channel-1",
          source: "intel_channel",
          source_instance: "wc.Venal+Br+Te",
          system_name: "0-UVHJ",
          raw_text: "0-UVHJ hostile movement",
          active: true,
          seen_count: 1,
        },
        {
          id: "ocr-1",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "NCG-PW",
          name: "Observed Pilot",
          active: true,
          seen_count: 9,
          metadata: {
            contact_standing: -5,
          },
        },
      ],
      map: {
        ...bootstrap.map,
        systems: [
          {
            name: "0-UVHJ",
            system_id: 30003615,
            x: 100,
            y: 120,
            hostile_count: 1,
            report_count: 1,
            security: -0.1,
          },
          {
            name: "NCG-PW",
            system_id: 30003616,
            x: 180,
            y: 150,
            hostile_count: 0,
            report_count: 0,
            security: -0.3,
          },
        ],
      },
    });

    expect(graph.nodes.find((node) => node.name === "0-UVHJ")).toMatchObject({
      hostileCount: 0,
      channelIntelCount: 1,
      observationCount: 1,
      hasAlerts: false,
      threatLevel: "unknown",
    });
    expect(graph.nodes.find((node) => node.name === "NCG-PW")).toMatchObject({
      hostileCount: 1,
      channelIntelCount: 0,
      observationCount: 1,
      hasAlerts: true,
    });
  });

  it("does not count friendly OCR active intel as hostile activity", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      active_intel: [
        {
          id: "ocr-friendly",
          source: "eve-sentry-detector",
          source_instance: "EVE - Hajimi6",
          system_name: "NCG-PW",
          name: "Hajimi6",
          active: true,
          seen_count: 1,
          metadata: {
            contact_standing: 10,
            standing_source: "esi_self",
            standing_contact_type: "character",
          },
        },
      ],
      map: {
        ...bootstrap.map,
        systems: [
          {
            name: "NCG-PW",
            system_id: 30003616,
            x: 180,
            y: 150,
            hostile_count: 0,
            report_count: 0,
            security: -0.3,
          },
        ],
      },
    });

    expect(graph.nodes[0]).toMatchObject({
      hostileCount: 0,
      reportCount: 0,
      observationCount: 0,
      hasAlerts: false,
    });
  });

  it("uses only the one-hour recent kill count for map loss heat", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: [
          {
            name: "0-UVHJ",
            system_id: 30003615,
            x: 100,
            y: 120,
            hostile_count: 0,
            report_count: 0,
            recent_kill_count: 2,
            kill_count: 12,
            security: -0.1,
          },
          {
            name: "NCG-PW",
            system_id: 30003616,
            x: 180,
            y: 150,
            hostile_count: 0,
            report_count: 0,
            kill_count: 9,
            security: -0.3,
          },
        ],
      },
    });

    expect(graph.nodes.find((node) => node.name === "0-UVHJ")).toMatchObject({
      killCount: 2,
    });
    expect(graph.nodes.find((node) => node.name === "NCG-PW")).toMatchObject({
      killCount: 0,
    });
  });

  it("marks only online monitoring clients on their current systems", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      clients: {
        count: 3,
        heartbeats: [
          {
            client_id: "detector-client:tenal-1",
            client_type: "detector_client",
            label: "Tenal OCR Monitor",
            online: true,
            details: {
              monitoring: true,
              system: "0-UVHJ",
            },
          },
          {
            client_id: "detector-client:tenal-2",
            client_type: "detector_client",
            label: "NCG Monitor",
            online: false,
            details: {
              monitoring: true,
              system_id: 30003616,
            },
          },
          {
            client_id: "detector-client:tenal-3",
            client_type: "detector_client",
            label: "Idle Monitor",
            online: true,
            details: {
              monitoring: false,
              system_id: 30003616,
            },
          },
        ],
        summary: {
          count: 3,
          online_count: 2,
          stale_count: 1,
        },
      },
    });

    expect(graph.nodes.find((node) => node.name === "0-UVHJ")).toMatchObject({
      monitorCount: 1,
      monitorOnlineCount: 1,
      monitorLabels: ["Tenal OCR Monitor"],
    });
    expect(graph.nodes.find((node) => node.name === "NCG-PW")).toMatchObject({
      monitorCount: 0,
      monitorOnlineCount: 0,
      monitorLabels: [],
    });
  });

  it("treats a legacy bootstrap without clients as having no monitors", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      clients: undefined,
    } as unknown as BootstrapPayload);

    expect(graph.nodes.every((node) => node.monitorOnlineCount === 0)).toBe(true);
  });
});
