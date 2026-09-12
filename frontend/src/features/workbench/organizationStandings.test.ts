import { expect, it } from "vitest";
import { buildPilotObservations } from "./observations";
import { buildTacticalGraph } from "./tacticalGraph";
import type { BootstrapPayload } from "./types";

it("does not reuse personal standing while organization relationships are unknown", () => {
  const payload = {
    schema_version: "intel_bootstrap.v1",
    generated_at: "2026-09-12T12:00:00Z",
    map: { systems: [{ name: "Tama", system_id: 30002813, x: 0, y: 0 }], links: [], summary: {} },
    reports: [], observations: [], alerts: [],
    active_intel: [{ id: "pending", name: "Pilot", source: "local_ocr", active: true,
      system_name: "Tama", system_id: 30002813,
      metadata: { standing_source: "esi_organization_pending", contact_standing: -10 } }],
  } as unknown as BootstrapPayload;
  expect(buildPilotObservations(payload)).toEqual([]);
  const graph = buildTacticalGraph(payload);
  expect(graph.nodes.length).toBeGreaterThan(0);
  expect(graph.nodes.every((node) => node.hostileCount === 0)).toBe(true);
});
