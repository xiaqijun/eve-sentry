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
      observationCount: 4,
      hasAlerts: true,
      isSelected: true,
      threatLevel: "high",
      threatScore: 80,
    });
  });

  it("marks deployed monitoring clients on their current systems", () => {
    const graph = buildTacticalGraph({
      ...bootstrap,
      clients: {
        count: 2,
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
        ],
        summary: {
          count: 2,
          online_count: 1,
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
      monitorCount: 1,
      monitorOnlineCount: 0,
      monitorLabels: ["NCG Monitor"],
    });
  });
});
