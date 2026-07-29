import { describe, expect, it } from "vitest";

import {
  HOSTILE_SUMMARY_CARD_HEIGHT,
  HOSTILE_SUMMARY_CARD_WIDTH,
  layoutHostileSummaryNodes,
} from "./hostileCardLayout";
import type { TacticalGraphNode } from "./tacticalGraph";

function node(
  id: string,
  x: number,
  y: number,
  overrides: Partial<TacticalGraphNode> = {},
): TacticalGraphNode {
  return {
    id,
    name: id,
    kind: "system",
    systemId: 1,
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
    hasAlerts: false,
    isSelected: false,
    threatLevel: "unknown",
    threatScore: null,
    ...overrides,
  };
}

const identityAdapter = {
  graphToScreen: (x: number, y: number) => ({ x, y }),
  screenToGraph: (x: number, y: number) => ({ x, y }),
};

describe("layoutHostileSummaryNodes", () => {
  it("places summaries around nearby systems without card collisions", () => {
    const firstSystem = node("A", 300, 250);
    const secondSystem = node("B", 310, 250);
    const firstSummary = node("hostile-summary:A", 300, 250, {
      kind: "hostile-summary",
      hostileAnchorX: 300,
      hostileAnchorY: 250,
      hostileCount: 3,
      threatLevel: "critical",
    });
    const secondSummary = node("hostile-summary:B", 310, 250, {
      kind: "hostile-summary",
      hostileAnchorX: 310,
      hostileAnchorY: 250,
      hostileCount: 2,
      threatLevel: "high",
    });

    layoutHostileSummaryNodes(
      [firstSystem, secondSystem, firstSummary, secondSummary],
      900,
      560,
      1,
      identityAdapter,
    );

    expect(firstSummary.hostileCardHidden).toBe(false);
    expect(secondSummary.hostileCardHidden).toBe(false);
    const separatedHorizontally = Math.abs(firstSummary.x - secondSummary.x) >=
      HOSTILE_SUMMARY_CARD_WIDTH + 10;
    const separatedVertically = Math.abs(firstSummary.y - secondSummary.y) >=
      HOSTILE_SUMMARY_CARD_HEIGHT + 10;
    expect(separatedHorizontally || separatedVertically).toBe(true);
  });

  it("limits summaries to eight while preserving a selected system", () => {
    const summaries = Array.from({ length: 9 }, (_, index) =>
      node(`hostile-summary:${index}`, 300 + index * 500, 400, {
        kind: "hostile-summary",
        hostileAnchorX: 300 + index * 500,
        hostileAnchorY: 400,
        hostileCount: 1,
        isSelected: index === 8,
        threatLevel: "low",
      }),
    );

    layoutHostileSummaryNodes(summaries, 5000, 900, 1, identityAdapter);

    expect(summaries.filter((item) => !item.hostileCardHidden)).toHaveLength(8);
    expect(summaries[8].hostileCardHidden).toBe(false);
  });
});
