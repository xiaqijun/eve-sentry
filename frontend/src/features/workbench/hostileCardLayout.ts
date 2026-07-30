import type { TacticalGraphNode } from "./tacticalGraph";

export const HOSTILE_SUMMARY_CARD_WIDTH = 160;
export const HOSTILE_SUMMARY_CARD_HEIGHT = 64;
export const HOSTILE_SUMMARY_MIN_SCALE = 0.26;
const MAX_VISIBLE_SUMMARIES = 8;
const CARD_GAP = 10;
const VIEWPORT_MARGIN = 12;

interface Point {
  x: number;
  y: number;
}

export interface HostileLayoutRect extends Point {
  height: number;
  width: number;
}

interface CoordinateAdapter {
  graphToScreen: (x: number, y: number) => Point;
  screenToGraph: (x: number, y: number) => Point;
}

const THREAT_RANK = {
  unknown: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
} as const;

function rectFromCenter(point: Point, width: number, height: number): HostileLayoutRect {
  return {
    x: point.x - width / 2,
    y: point.y - height / 2,
    width,
    height,
  };
}

function overlaps(left: HostileLayoutRect, right: HostileLayoutRect, gap = 0): boolean {
  return left.x < right.x + right.width + gap &&
    left.x + left.width + gap > right.x &&
    left.y < right.y + right.height + gap &&
    left.y + left.height + gap > right.y;
}

function insideViewport(rect: HostileLayoutRect, width: number, height: number): boolean {
  return rect.x >= VIEWPORT_MARGIN &&
    rect.y >= VIEWPORT_MARGIN &&
    rect.x + rect.width <= width - VIEWPORT_MARGIN &&
    rect.y + rect.height <= height - VIEWPORT_MARGIN;
}

function candidateCenters(anchor: Point, width: number): Point[] {
  const inward = anchor.x < width / 2 ? 1 : -1;
  const horizontal = HOSTILE_SUMMARY_CARD_WIDTH / 2 + 30;
  const vertical = HOSTILE_SUMMARY_CARD_HEIGHT / 2 + 32;
  return [
    { x: anchor.x + inward * horizontal, y: anchor.y },
    { x: anchor.x + inward * horizontal, y: anchor.y - vertical },
    { x: anchor.x + inward * horizontal, y: anchor.y + vertical },
    { x: anchor.x, y: anchor.y - vertical },
    { x: anchor.x, y: anchor.y + vertical },
    { x: anchor.x - inward * horizontal, y: anchor.y },
    { x: anchor.x - inward * horizontal, y: anchor.y - vertical },
    { x: anchor.x - inward * horizontal, y: anchor.y + vertical },
  ];
}

function summaryPriority(left: TacticalGraphNode, right: TacticalGraphNode): number {
  return Number(right.isSelected) - Number(left.isSelected) ||
    THREAT_RANK[right.threatLevel] - THREAT_RANK[left.threatLevel] ||
    right.hostileCount - left.hostileCount ||
    left.id.localeCompare(right.id);
}

export function layoutHostileSummaryNodes(
  nodes: TacticalGraphNode[],
  viewportWidth: number,
  viewportHeight: number,
  globalScale: number,
  adapter: CoordinateAdapter,
  reservedAreas: HostileLayoutRect[] = [],
): void {
  const summaries = nodes
    .filter((node) => node.kind === "hostile-summary")
    .sort(summaryPriority);

  if (globalScale < HOSTILE_SUMMARY_MIN_SCALE) {
    summaries.forEach((node) => {
      node.hostileCardHidden = true;
    });
    return;
  }

  const occupied = [...reservedAreas];
  nodes.forEach((node) => {
    if (node.kind === "hostile-summary") {
      return;
    }
    occupied.push(rectFromCenter(adapter.graphToScreen(node.x, node.y), 36, 36));
  });

  summaries.forEach((node, index) => {
    const anchorX = node.hostileAnchorX ?? node.x;
    const anchorY = node.hostileAnchorY ?? node.y;
    const anchor = adapter.graphToScreen(anchorX, anchorY);
    const placement = index < MAX_VISIBLE_SUMMARIES
      ? candidateCenters(anchor, viewportWidth).find((candidate) => {
          const rect = rectFromCenter(
            candidate,
            HOSTILE_SUMMARY_CARD_WIDTH,
            HOSTILE_SUMMARY_CARD_HEIGHT,
          );
          return insideViewport(rect, viewportWidth, viewportHeight) &&
            occupied.every((current) => !overlaps(rect, current, CARD_GAP));
        })
      : undefined;

    if (!placement) {
      node.hostileCardHidden = true;
      return;
    }

    const rect = rectFromCenter(
      placement,
      HOSTILE_SUMMARY_CARD_WIDTH,
      HOSTILE_SUMMARY_CARD_HEIGHT,
    );
    const graphPoint = adapter.screenToGraph(placement.x, placement.y);
    node.x = graphPoint.x;
    node.y = graphPoint.y;
    node.fx = graphPoint.x;
    node.fy = graphPoint.y;
    node.hostileCardHidden = false;
    occupied.push(rect);
  });
}
