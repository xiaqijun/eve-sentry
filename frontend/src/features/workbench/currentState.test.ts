import { expect, it } from "vitest";
import { buildTacticalGraph } from "./tacticalGraph";
import type { BootstrapPayload } from "./types";

it("uses the newest canonical freshness ahead of an older map snapshot", () => {
  const payload = {
    map: { systems: [{ name: "Tama", system_id: 30002813, x: 0, y: 0, freshness: "unknown" }], links: [], summary: {} },
    reports: [], observations: [], alerts: [],
    active_intel: [{ id: "presence", source: "eve-sentry-detector", active: true,
      system_name: "Tama", target_type: "system",
      metadata: { presence_only: true, hostile_icon_count: 2, state_version: 42, freshness: "fresh" } }],
  } as unknown as BootstrapPayload;
  const system = buildTacticalGraph(payload).nodes.find(node => node.name === "Tama");
  expect(system?.freshness).toBe("fresh");
  expect(system?.hostileCount).toBe(2);
});

it("keeps topology without old enemies, cards, or monitoring markers", () => {
  const payload = {
    map: { systems: [{ name: "Tama", system_id: 30002813, x: 0, y: 0,
      hostile_count: 9, freshness: "unknown" }], links: [], summary: {} },
    reports: [], observations: [], alerts: [],
    clients: { heartbeats: [{ online: true, details: { monitoring: true,
      targets: [{ system_name: "Tama", monitoring: true, capture_online: false }] } }] },
    active_intel: [{ id: "presence", source: "eve-sentry-detector", active: true,
      system_name: "Tama", target_type: "system",
      metadata: { presence_only: true, hostile_icon_count: 9, state_version: 42, freshness: "unknown" } }],
  } as unknown as BootstrapPayload;
  const graph = buildTacticalGraph(payload, null, { includeHostileCards: true });
  expect(graph.nodes).toHaveLength(1);
  expect(graph.nodes[0].hostileCount).toBe(0);
  expect(graph.nodes[0].monitorCount).toBe(0);
  expect(graph.nodes[0].hasAlerts).toBe(false);
  expect(graph.nodes[0].freshness).toBeUndefined();
});
