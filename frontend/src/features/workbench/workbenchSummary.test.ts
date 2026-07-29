import { describe, expect, it } from "vitest";

import type { BootstrapPayload } from "./types";
import { summarizeWorkbench } from "./workbenchSummary";

const bootstrap: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1",
  generated_at: "2026-07-02T10:00:00+00:00",
  map: {
    schema_version: "map_snapshot.v1",
    generated_at: "2026-07-02T10:00:00+00:00",
    systems: [
      {
        name: "Tama",
        system_id: 30002813,
        x: 10,
        y: 20,
        hostile_count: 2,
        report_count: 3,
        hostiles: ["Alice", "Bob"],
      },
      {
        name: "Kedama",
        system_id: 30002819,
        x: 30,
        y: 40,
        hostile_count: 0,
        report_count: 1,
        hostiles: [],
      },
    ],
    links: [{ from: "Tama", to: "Kedama" }],
    summary: {
      system_count: 2,
      hostile_count: 2,
      report_count: 4,
      alert_count: 1,
    },
  },
  reports: [],
  alerts: [],
  clients: {
    count: 1,
    heartbeats: [],
    summary: {
      count: 1,
      online_count: 1,
      stale_count: 0,
      by_type: { alert_client: 1 },
      by_status: { running: 1 },
    },
  },
  config: {
    schema_version: "scoring_config.v1",
  },
  esi: {
    enabled: false,
    authenticated: false,
  },
};

describe("summarizeWorkbench", () => {
  it("derives top-line workbench summary from bootstrap payload", () => {
    expect(summarizeWorkbench(bootstrap)).toEqual({
      systems: 2,
      hostiles: 2,
      reports: 4,
      alerts: 1,
      onlineClients: 1,
    });
  });

  it("preserves explicit zero counts instead of falling back to array lengths", () => {
    expect(summarizeWorkbench({
      ...bootstrap,
      map: {
        ...bootstrap.map,
        summary: {
          system_count: 0,
          hostile_count: 0,
          report_count: 0,
          alert_count: 0,
        },
      },
    })).toEqual({
      systems: 0,
      hostiles: 0,
      reports: 0,
      alerts: 0,
      onlineClients: 1,
    });
  });

  it("defaults online clients to zero for a legacy bootstrap without clients", () => {
    expect(summarizeWorkbench({
      ...bootstrap,
      clients: undefined,
    } as unknown as BootstrapPayload).onlineClients).toBe(0);
  });
});
