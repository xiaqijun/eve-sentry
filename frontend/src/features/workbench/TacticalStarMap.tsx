import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type NodeObject,
} from "react-force-graph-2d";

import type {
  TacticalGraphData,
  TacticalGraphLink,
  TacticalGraphNode,
  TacticalHostileIntel,
} from "./tacticalGraph";

interface TacticalStarMapProps {
  fitSignal?: number;
  graphData: TacticalGraphData;
  onSelectSystem: (systemId: number | null) => void;
}

const HOSTILE_CARD_WIDTH = 232;
const HOSTILE_CARD_HEIGHT = 94;
const HOSTILE_PORTRAIT_SIZE = 62;
const portraitCache = new Map<number, HTMLImageElement>();

const THREAT_STYLE: Record<
  TacticalHostileIntel["threatLevel"],
  { background: string; foreground: string; label: string }
> = {
  critical: { background: "#fde8e7", foreground: "#b42318", label: "严重" },
  high: { background: "#fff0e8", foreground: "#c2410c", label: "高危" },
  medium: { background: "#fff7d6", foreground: "#9a6700", label: "中危" },
  low: { background: "#e8f3ff", foreground: "#1769aa", label: "低危" },
  unknown: { background: "#eef2f1", foreground: "#66736d", label: "未知" },
};

function portraitFor(characterId: number | null): HTMLImageElement | null {
  if (characterId === null || typeof Image === "undefined") {
    return null;
  }
  const cached = portraitCache.get(characterId);
  if (cached) {
    return cached;
  }
  const image = new Image();
  image.decoding = "async";
  image.src = `https://images.evetech.net/characters/${characterId}/portrait?size=64`;
  portraitCache.set(characterId, image);
  return image;
}

function fitCanvasText(
  context: CanvasRenderingContext2D,
  value: string,
  maxWidth: number,
): string {
  if (context.measureText(value).width <= maxWidth) {
    return value;
  }
  let result = value;
  while (result.length > 1 && context.measureText(`${result}...`).width > maxWidth) {
    result = result.slice(0, -1);
  }
  return `${result}...`;
}

function drawHostilePortrait(
  intel: TacticalHostileIntel,
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
): void {
  context.fillStyle = "#e8eeeb";
  context.fillRect(x, y, HOSTILE_PORTRAIT_SIZE, HOSTILE_PORTRAIT_SIZE);
  const portrait = portraitFor(intel.characterId);
  if (portrait?.complete && portrait.naturalWidth > 0) {
    context.drawImage(
      portrait,
      x,
      y,
      HOSTILE_PORTRAIT_SIZE,
      HOSTILE_PORTRAIT_SIZE,
    );
  } else {
    context.fillStyle = "#73817a";
    context.font = '700 20px "Segoe UI", "Microsoft YaHei", sans-serif';
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(intel.name.slice(0, 1).toUpperCase(), x + 31, y + 32);
  }
  context.strokeStyle = "#d7dfdb";
  context.lineWidth = 1;
  context.strokeRect(x, y, HOSTILE_PORTRAIT_SIZE, HOSTILE_PORTRAIT_SIZE);
}

function drawHostileCard(
  node: NodeObject<TacticalGraphNode>,
  context: CanvasRenderingContext2D,
  globalScale: number,
): void {
  const intel = node.hostileIntel;
  if (!intel) {
    return;
  }
  const x = -HOSTILE_CARD_WIDTH / 2;
  const y = -HOSTILE_CARD_HEIGHT / 2;
  const useVerticalLayout = globalScale < 0.5 &&
    typeof node.hostileAnchorX === "number" &&
    typeof node.hostileAnchorY === "number";
  const cardX = useVerticalLayout
    ? Number(node.hostileAnchorX)
    : Number(node.x || 0);
  const cardY = useVerticalLayout
    ? Number(node.hostileAnchorY) - 108 / globalScale
    : Number(node.y || 0);
  const threat = THREAT_STYLE[intel.threatLevel];
  const score = intel.threatScore === null
    ? threat.label
    : `${threat.label} ${Math.round(intel.threatScore)}`;

  context.save();
  context.translate(cardX, cardY);
  context.scale(1 / globalScale, 1 / globalScale);
  context.shadowColor = "rgba(44, 62, 55, 0.16)";
  context.shadowBlur = 12;
  context.shadowOffsetY = 4;
  context.fillStyle = "rgba(255, 255, 255, 0.98)";
  context.fillRect(x, y, HOSTILE_CARD_WIDTH, HOSTILE_CARD_HEIGHT);
  context.shadowColor = "transparent";
  context.strokeStyle = node.isSelected ? "#23845f" : "#d8e0dc";
  context.lineWidth = node.isSelected ? 2 : 1;
  context.strokeRect(x, y, HOSTILE_CARD_WIDTH, HOSTILE_CARD_HEIGHT);
  context.fillStyle = "#d6453d";
  context.fillRect(x, y, 4, HOSTILE_CARD_HEIGHT);

  drawHostilePortrait(intel, context, x + 12, y + 16);

  context.font = '700 13px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "left";
  context.textBaseline = "middle";
  context.fillStyle = "#202c27";
  context.fillText(fitCanvasText(context, intel.name, 92), x + 86, y + 20);

  const threatWidth = Math.max(40, context.measureText(score).width + 12);
  context.fillStyle = threat.background;
  context.fillRect(x + HOSTILE_CARD_WIDTH - threatWidth - 10, y + 10, threatWidth, 20);
  context.font = '700 9px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "center";
  context.fillStyle = threat.foreground;
  context.fillText(score, x + HOSTILE_CARD_WIDTH - threatWidth / 2 - 10, y + 20);

  const detailX = x + 86;
  context.font = '700 8px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "center";
  context.fillStyle = "#eaf1ee";
  context.fillRect(detailX, y + 40, 18, 15);
  context.fillRect(detailX, y + 65, 18, 15);
  context.fillStyle = "#587067";
  context.fillText("军", detailX + 9, y + 47.5);
  context.fillText("联", detailX + 9, y + 72.5);

  context.font = '500 10px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "left";
  context.fillStyle = "#56655f";
  context.fillText(
    fitCanvasText(context, intel.corporation, 112),
    detailX + 24,
    y + 47.5,
  );
  context.fillText(
    fitCanvasText(context, intel.alliance, 112),
    detailX + 24,
    y + 72.5,
  );

  context.fillStyle = "#edf1ef";
  context.fillRect(x + 4, y + HOSTILE_CARD_HEIGHT - 3, HOSTILE_CARD_WIDTH - 4, 3);
  context.fillStyle = threat.foreground;
  context.fillRect(
    x + 4,
    y + HOSTILE_CARD_HEIGHT - 3,
    (HOSTILE_CARD_WIDTH - 4) * Math.min(1, Math.max(0.08, (intel.threatScore ?? 35) / 100)),
    3,
  );
  context.restore();
}

function nodeColor(node: TacticalGraphNode): string {
  if (node.hostileCount > 0) {
    return "#d6453d";
  }
  if ((node.killCount ?? 0) > 0) {
    return "#b98528";
  }
  if (node.channelIntelCount > 0 || node.observationCount > 0) {
    return "#2f86a5";
  }
  if (node.monitorOnlineCount > 0) {
    return "#2f956d";
  }
  return "#7b898d";
}

function drawGateLink(
  link: TacticalGraphLink,
  context: CanvasRenderingContext2D,
  globalScale: number = 1,
): void {
  const source = graphNode(link.source);
  const target = graphNode(link.target);
  if (!source || !target) {
    return;
  }

  const isLossHot = (source.killCount ?? 0) > 0 || (target.killCount ?? 0) > 0;
  const isHot = source.hostileCount > 0 || target.hostileCount > 0;
  const isSelected = source.isSelected || target.isSelected;
  const x1 = Number(source.x || 0);
  const y1 = Number(source.y || 0);
  const x2 = Number(target.x || 0);
  const y2 = Number(target.y || 0);

  context.save();
  context.lineCap = "round";
  if (link.kind === "hostile-intel") {
    const useVerticalLayout = globalScale < 0.5 &&
      typeof target.hostileAnchorX === "number" &&
      typeof target.hostileAnchorY === "number";
    const targetX = useVerticalLayout ? Number(target.hostileAnchorX) : x2;
    const targetY = useVerticalLayout
      ? Number(target.hostileAnchorY) - 108 / globalScale
      : y2;
    const midpointX = useVerticalLayout ? x1 : x1 + (targetX - x1) * 0.52;
    const midpointY = useVerticalLayout ? y1 + (targetY - y1) * 0.45 : y1;
    context.beginPath();
    context.moveTo(x1, y1);
    context.lineTo(midpointX, midpointY);
    context.lineTo(midpointX, targetY);
    context.lineTo(targetX, targetY);
    context.strokeStyle = "rgba(214, 69, 61, 0.72)";
    context.lineWidth = Math.max(1.4, 1.6 / globalScale);
    context.setLineDash?.([5 / globalScale, 4 / globalScale]);
    context.stroke();
    context.setLineDash?.([]);
    context.beginPath();
    context.arc(midpointX, targetY, 2.4 / globalScale, 0, Math.PI * 2);
    context.fillStyle = "#d6453d";
    context.fill();
    context.restore();
    return;
  }
  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.strokeStyle = isHot
    ? "rgba(214, 69, 61, 0.65)"
    : isLossHot
      ? "rgba(185, 133, 40, 0.55)"
      : isSelected
        ? "rgba(23, 107, 80, 0.65)"
        : "rgba(91, 112, 119, 0.28)";
  context.lineWidth = isSelected ? 2.2 : isHot || isLossHot ? 1.5 : 1;
  context.stroke();
  context.restore();
}

function drawNode(
  node: NodeObject<TacticalGraphNode>,
  context: CanvasRenderingContext2D,
  globalScale: number,
): void {
  if (node.kind === "hostile-card") {
    drawHostileCard(node, context, globalScale);
    return;
  }
  const label = node.name;
  const color = nodeColor(node);
  const lossCount = Math.max(0, node.killCount ?? 0);
  const isActive = node.hostileCount > 0 || lossCount > 0 || node.channelIntelCount > 0;
  const radius = node.isSelected ? 7.4 : node.hostileCount > 0 ? 6.5 : isActive ? 5.6 : 4.6;
  const fontSize = Math.max(8, 11 / globalScale);
  const x = Number(node.x || 0);
  const y = Number(node.y || 0);

  context.save();
  if (node.isSelected || node.hostileCount > 0) {
    context.beginPath();
    context.arc(x, y, radius + (node.isSelected ? 9 : 7), 0, Math.PI * 2);
    context.fillStyle = node.isSelected
      ? "rgba(23, 107, 80, 0.08)"
      : "rgba(214, 69, 61, 0.08)";
    context.fill();
    context.strokeStyle = node.isSelected
      ? "rgba(23, 107, 80, 0.28)"
      : "rgba(214, 69, 61, 0.24)";
    context.lineWidth = 1;
    context.stroke();
  }

  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
  context.lineWidth = node.isSelected ? 2 : 1.2;
  context.strokeStyle = node.isSelected ? "#176b50" : "rgba(255,255,255,0.96)";
  context.stroke();

  if (globalScale >= 0.5 || node.hostileCount > 0 || node.isSelected) {
    context.font = `600 ${fontSize}px "Segoe UI", "Microsoft YaHei", sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "top";
    context.fillStyle = node.isSelected || isActive ? "#26342e" : "#697771";
    context.fillText(label, x, y + radius + 3);
  }

  if (node.hostileCount > 0 || lossCount > 0) {
    const badgeText = node.hostileCount > 0 ? String(node.hostileCount) : String(lossCount);
    const badgeRadius = Math.max(4.2, 5.2 / globalScale);
    const badgeX = x + radius + 2;
    const badgeY = y - radius - 2;
    context.beginPath();
    context.arc(badgeX, badgeY, badgeRadius, 0, Math.PI * 2);
    context.fillStyle = node.hostileCount > 0 ? "#bb342d" : "#9c701c";
    context.fill();
    context.font = `700 ${Math.max(6, 7 / globalScale)}px "Segoe UI", sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillStyle = "#ffffff";
    context.fillText(badgeText, badgeX, badgeY + 0.3);
  }
  context.restore();
}

function paintNodePointerArea(
  node: NodeObject<TacticalGraphNode>,
  color: string,
  context: CanvasRenderingContext2D,
): void {
  context.fillStyle = color;
  if (node.kind === "hostile-card") {
    context.fillRect(
      Number(node.x || 0) - HOSTILE_CARD_WIDTH / 2,
      Number(node.y || 0) - HOSTILE_CARD_HEIGHT / 2,
      HOSTILE_CARD_WIDTH,
      HOSTILE_CARD_HEIGHT,
    );
    return;
  }
  context.beginPath();
  context.arc(Number(node.x || 0), Number(node.y || 0), 18, 0, Math.PI * 2);
  context.fill();
}

function graphNode(value: unknown): TacticalGraphNode | null {
  return typeof value === "object" && value !== null && "id" in value
    ? (value as TacticalGraphNode)
    : null;
}

export function TacticalStarMap({
  fitSignal = 0,
  graphData,
  onSelectSystem,
}: TacticalStarMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef =
    useRef<ForceGraphMethods<TacticalGraphNode, TacticalGraphLink>>();
  const [size, setSize] = useState({ height: 560, width: 900 });

  useEffect(() => {
    const element = containerRef.current;
    if (!element || typeof ResizeObserver === "undefined") {
      return undefined;
    }
    const observer = new ResizeObserver(([entry]) => {
      setSize({
        height: Math.max(420, entry.contentRect.height),
        width: Math.max(280, entry.contentRect.width),
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const hasHostileCards = graphData.nodes.some(
    (node) => node.kind === "hostile-card",
  );
  const fitPadding = hasHostileCards
    ? Math.min(260, Math.max(128, size.width * 0.25))
    : Math.min(96, Math.max(48, size.width * 0.15));
  const fitGraph = useCallback((duration = 650) => {
    graphRef.current?.zoomToFit(duration, fitPadding);
  }, [fitPadding]);
  const hasGraphData = graphData.nodes.length > 0;

  useEffect(() => {
    if (!hasGraphData) {
      return undefined;
    }
    const quickFit = window.setTimeout(() => fitGraph(0), 80);
    const settledFit = window.setTimeout(() => fitGraph(650), 360);
    return () => {
      window.clearTimeout(quickFit);
      window.clearTimeout(settledFit);
    };
  }, [fitGraph, fitSignal, hasGraphData]);

  const linkColor = useMemo(() => "rgba(91, 112, 119, 0.28)", []);

  return (
    <div className="tactical-star-map" data-testid="tactical-star-map" ref={containerRef}>
      {graphData.nodes.length === 0 ? (
        <div className="tactical-star-map-empty">暂无实时敌对目标</div>
      ) : null}
      <ForceGraph2D<TacticalGraphNode, TacticalGraphLink>
        ref={graphRef}
        graphData={graphData}
        nodeId="id"
        linkSource="source"
        linkTarget="target"
        width={size.width}
        height={size.height}
        backgroundColor="rgba(0,0,0,0)"
        cooldownTicks={0}
        enableNodeDrag={false}
        enablePanInteraction
        enablePointerInteraction
        enableZoomInteraction
        linkColor={linkColor}
        linkDirectionalParticles={(link) =>
          link.kind === "hostile-intel"
            ? 2
            : (graphNode(link.source)?.hostileCount || 0) > 0
              ? 1
              : 0
        }
        linkDirectionalParticleColor={() => "rgba(214, 69, 61, 0.82)"}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleWidth={1.8}
        linkCanvasObject={drawGateLink}
        linkCanvasObjectMode={() => "replace"}
        linkWidth={(link) => {
          const source = graphNode(link.source);
          const target = graphNode(link.target);
          return source?.isSelected || target?.isSelected
            ? 1.9
            : 1.05;
        }}
        maxZoom={9}
        minZoom={0.12}
        nodeCanvasObject={drawNode}
        nodePointerAreaPaint={paintNodePointerArea}
        onNodeClick={(node) =>
          onSelectSystem(typeof node.systemId === "number" ? node.systemId : null)
        }
      />
    </div>
  );
}
