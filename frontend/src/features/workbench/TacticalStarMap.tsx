import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Avatar, Drawer, Empty, List, Tag } from "@arco-design/web-react";
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
import {
  HOSTILE_SUMMARY_CARD_HEIGHT,
  HOSTILE_SUMMARY_CARD_WIDTH,
  layoutHostileSummaryNodes,
  type HostileLayoutRect,
} from "./hostileCardLayout";

interface TacticalStarMapProps {
  fitSignal?: number;
  graphData: TacticalGraphData;
  onSelectSystem: (systemId: number | null) => void;
}

const HOSTILE_PORTRAIT_SIZE = 48;
const HOSTILE_CARD_DRAW_SCALE = 0.8;
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

function animationTime(): number {
  return typeof performance === "undefined" ? 0 : performance.now();
}

function nodePhase(node: TacticalGraphNode, timeMs: number, durationMs: number): number {
  const seed = [...node.id].reduce((sum, character) => sum + character.charCodeAt(0), 0);
  return ((timeMs + seed * 37) % durationMs) / durationMs;
}

function nodePulseColor(node: TacticalGraphNode): string | null {
  if (node.hostileCount > 0) {
    return "214, 69, 61";
  }
  if ((node.killCount ?? 0) > 0) {
    return "185, 133, 40";
  }
  if (node.channelIntelCount > 0 || node.observationCount > 0) {
    return "47, 134, 165";
  }
  if (node.monitorOnlineCount > 0 || node.isSelected) {
    return "47, 149, 109";
  }
  return null;
}

function drawNodePulse(
  node: TacticalGraphNode,
  context: CanvasRenderingContext2D,
  globalScale: number,
  radius: number,
  timeMs: number,
): void {
  const color = nodePulseColor(node);
  if (!color) {
    return;
  }
  const pulseCount = node.hostileCount > 0 ? 2 : 1;
  const basePhase = nodePhase(node, timeMs, node.hostileCount > 0 ? 1500 : 2400);
  const x = Number(node.x || 0);
  const y = Number(node.y || 0);
  for (let index = 0; index < pulseCount; index += 1) {
    const phase = (basePhase + index / pulseCount) % 1;
    context.beginPath();
    context.arc(
      x,
      y,
      radius + (7 + phase * 16) / globalScale,
      0,
      Math.PI * 2,
    );
    context.strokeStyle = `rgba(${color}, ${0.3 * (1 - phase)})`;
    context.lineWidth = Math.max(0.7, 1.2 / globalScale);
    context.stroke();
  }
}

function drawHostilePortrait(
  intel: TacticalHostileIntel | undefined,
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
): void {
  context.fillStyle = "#e8eeeb";
  context.fillRect(x, y, HOSTILE_PORTRAIT_SIZE, HOSTILE_PORTRAIT_SIZE);
  const portrait = portraitFor(intel?.characterId ?? null);
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
    context.fillText((intel?.name || "?").slice(0, 1).toUpperCase(), x + 24, y + 24);
  }
  context.strokeStyle = "#d7dfdb";
  context.lineWidth = 1;
  context.strokeRect(x, y, HOSTILE_PORTRAIT_SIZE, HOSTILE_PORTRAIT_SIZE);
}

function drawHostileCard(
  node: NodeObject<TacticalGraphNode>,
  context: CanvasRenderingContext2D,
  globalScale: number,
  timeMs: number,
): void {
  const intel = node.hostileIntel;
  if (node.hostileCardHidden) {
    return;
  }
  const cardWidth = HOSTILE_SUMMARY_CARD_WIDTH / HOSTILE_CARD_DRAW_SCALE;
  const cardHeight = HOSTILE_SUMMARY_CARD_HEIGHT / HOSTILE_CARD_DRAW_SCALE;
  const x = -cardWidth / 2;
  const y = -cardHeight / 2;
  const cardX = Number(node.x || 0);
  const cardY = Number(node.y || 0);
  const threat = THREAT_STYLE[intel?.threatLevel || node.threatLevel];
  const threatScore = intel?.threatScore ?? node.threatScore;
  const score = threatScore === null
    ? threat.label
    : `${threat.label} ${Math.round(threatScore)}`;
  const hostileCount = Math.max(node.hostileCount, node.hostileMembers?.length || 0);
  const extraCount = Math.max(0, hostileCount - 1);
  const pilotName = intel?.name || "身份待解析";
  const corporation = intel?.corporation || "未解析军团";
  const alliance = intel?.alliance || `${hostileCount} 名敌对信号`;

  context.save();
  context.translate(cardX, cardY);
  context.scale(
    HOSTILE_CARD_DRAW_SCALE / globalScale,
    HOSTILE_CARD_DRAW_SCALE / globalScale,
  );
  context.shadowColor = "rgba(44, 62, 55, 0.16)";
  context.shadowBlur = 12;
  context.shadowOffsetY = 4;
  context.fillStyle = "rgba(255, 255, 255, 0.98)";
  context.fillRect(x, y, cardWidth, cardHeight);
  context.shadowColor = "transparent";
  context.strokeStyle = node.isSelected ? "#23845f" : "#d8e0dc";
  context.lineWidth = node.isSelected ? 2 : 1;
  context.strokeRect(x, y, cardWidth, cardHeight);
  context.fillStyle = "#d6453d";
  context.fillRect(x, y, 4, cardHeight);

  drawHostilePortrait(intel, context, x + 12, y + 16);

  context.font = '700 12px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "left";
  context.textBaseline = "middle";
  context.fillStyle = "#202c27";
  context.fillText(fitCanvasText(context, pilotName, 82), x + 70, y + 18);

  context.font = '700 9px "Segoe UI", "Microsoft YaHei", sans-serif';
  const threatWidth = Math.max(38, context.measureText(score).width + 10);
  context.fillStyle = threat.background;
  context.fillRect(x + cardWidth - threatWidth - 10, y + 9, threatWidth, 19);
  context.textAlign = "center";
  context.fillStyle = threat.foreground;
  context.fillText(score, x + cardWidth - threatWidth / 2 - 10, y + 18.5);

  context.font = '500 9px "Segoe UI", "Microsoft YaHei", sans-serif';
  context.textAlign = "left";
  context.fillStyle = "#56655f";
  context.fillText(
    fitCanvasText(context, corporation, 104),
    x + 70,
    y + 39,
  );
  context.fillText(
    fitCanvasText(context, alliance, extraCount > 0 ? 76 : 104),
    x + 70,
    y + 57,
  );

  if (extraCount > 0) {
    const badge = `+${extraCount}`;
    context.font = '700 9px "Segoe UI", "Microsoft YaHei", sans-serif';
    context.textAlign = "center";
    context.fillStyle = "#fbe9e7";
    context.fillRect(x + cardWidth - 42, y + 47, 30, 18);
    context.fillStyle = "#b42318";
    context.fillText(badge, x + cardWidth - 27, y + 56);
  }

  context.fillStyle = "#edf1ef";
  context.fillRect(x + 4, y + cardHeight - 3, cardWidth - 4, 3);
  context.fillStyle = threat.foreground;
  context.fillRect(
    x + 4,
    y + cardHeight - 3,
    (cardWidth - 4) * Math.min(1, Math.max(0.08, (threatScore ?? 35) / 100)),
    3,
  );
  const riskWidth = (cardWidth - 4) *
    Math.min(1, Math.max(0.08, (threatScore ?? 35) / 100));
  const shimmerWidth = 24;
  const shimmerStart = x + 4 - shimmerWidth +
    ((timeMs % 1800) / 1800) * (riskWidth + shimmerWidth);
  const shimmerLeft = Math.max(x + 4, shimmerStart);
  const shimmerRight = Math.min(x + 4 + riskWidth, shimmerStart + shimmerWidth);
  if (shimmerRight > shimmerLeft) {
    context.fillStyle = "rgba(255, 255, 255, 0.62)";
    context.fillRect(
      shimmerLeft,
      y + cardHeight - 3,
      shimmerRight - shimmerLeft,
      3,
    );
  }
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
    if (target.hostileCardHidden) {
      context.restore();
      return;
    }
    const deltaX = x2 - x1;
    const deltaY = y2 - y1;
    const horizontal = Math.abs(deltaX) >= Math.abs(deltaY);
    const targetX = horizontal
      ? x2 - Math.sign(deltaX || 1) * HOSTILE_SUMMARY_CARD_WIDTH / 2 / globalScale
      : x2;
    const targetY = horizontal
      ? y2
      : y2 - Math.sign(deltaY || 1) * HOSTILE_SUMMARY_CARD_HEIGHT / 2 / globalScale;
    const midpointX = x1 + (targetX - x1) * 0.52;
    const midpointY = y1 + (targetY - y1) * 0.52;
    context.beginPath();
    context.moveTo(x1, y1);
    context.lineTo(horizontal ? midpointX : x1, horizontal ? y1 : midpointY);
    context.lineTo(horizontal ? midpointX : targetX, horizontal ? targetY : midpointY);
    context.lineTo(targetX, targetY);
    context.strokeStyle = "rgba(214, 69, 61, 0.72)";
    context.lineWidth = Math.max(1.4, 1.6 / globalScale);
    context.setLineDash?.([5 / globalScale, 4 / globalScale]);
    context.stroke();
    context.setLineDash?.([]);
    context.beginPath();
    context.arc(targetX, targetY, 2.4 / globalScale, 0, Math.PI * 2);
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
  timeMs: number = 0,
): void {
  if (node.kind === "hostile-summary") {
    drawHostileCard(node, context, globalScale, timeMs);
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
  drawNodePulse(node, context, globalScale, radius, timeMs);
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
  globalScale: number = 1,
): void {
  context.fillStyle = color;
  if (node.kind === "hostile-summary") {
    if (node.hostileCardHidden) {
      return;
    }
    context.fillRect(
      Number(node.x || 0) - HOSTILE_SUMMARY_CARD_WIDTH / 2 / globalScale,
      Number(node.y || 0) - HOSTILE_SUMMARY_CARD_HEIGHT / 2 / globalScale,
      HOSTILE_SUMMARY_CARD_WIDTH / globalScale,
      HOSTILE_SUMMARY_CARD_HEIGHT / globalScale,
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

function linkActivityNodes(
  link: TacticalGraphLink,
): [TacticalGraphNode | null, TacticalGraphNode | null] {
  return [graphNode(link.source), graphNode(link.target)];
}

function linkParticleCount(link: TacticalGraphLink): number {
  if (link.kind === "hostile-intel") {
    return 3;
  }
  const [source, target] = linkActivityNodes(link);
  if ((source?.hostileCount || 0) > 0 || (target?.hostileCount || 0) > 0) {
    return 2;
  }
  if ((source?.monitorOnlineCount || 0) > 0 || (target?.monitorOnlineCount || 0) > 0) {
    return 1;
  }
  if (
    (source?.channelIntelCount || 0) > 0 ||
    (target?.channelIntelCount || 0) > 0 ||
    (source?.observationCount || 0) > 0 ||
    (target?.observationCount || 0) > 0 ||
    (source?.killCount || 0) > 0 ||
    (target?.killCount || 0) > 0
  ) {
    return 1;
  }
  return 0;
}

function linkParticleColor(link: TacticalGraphLink): string {
  if (link.kind === "hostile-intel") {
    return "rgba(214, 69, 61, 0.94)";
  }
  const [source, target] = linkActivityNodes(link);
  if ((source?.hostileCount || 0) > 0 || (target?.hostileCount || 0) > 0) {
    return "rgba(214, 69, 61, 0.82)";
  }
  if ((source?.killCount || 0) > 0 || (target?.killCount || 0) > 0) {
    return "rgba(185, 133, 40, 0.82)";
  }
  if (
    (source?.channelIntelCount || 0) > 0 ||
    (target?.channelIntelCount || 0) > 0 ||
    (source?.observationCount || 0) > 0 ||
    (target?.observationCount || 0) > 0
  ) {
    return "rgba(47, 134, 165, 0.82)";
  }
  return "rgba(47, 149, 109, 0.78)";
}

function linkParticleSpeed(link: TacticalGraphLink): number {
  return link.kind === "hostile-intel" ? 0.008 : 0.0035;
}

function linkParticleWidth(link: TacticalGraphLink): number {
  return link.kind === "hostile-intel" ? 2.4 : 1.7;
}

function portraitUrl(characterId: number | null): string | undefined {
  return characterId === null
    ? undefined
    : `https://images.evetech.net/characters/${characterId}/portrait?size=64`;
}

function visibleOverlayRects(container: HTMLDivElement | null): HostileLayoutRect[] {
  const stage = container?.closest(".star-map-stage");
  if (!container || !stage) {
    return [];
  }
  const canvasRect = container.getBoundingClientRect();
  return Array.from(
    stage.querySelectorAll<HTMLElement>(
      ".star-map-status, .star-map-legend, .star-map-tools, .star-map-selection",
    ),
  ).map((element) => {
    const rect = element.getBoundingClientRect();
    return {
      x: rect.left - canvasRect.left,
      y: rect.top - canvasRect.top,
      width: rect.width,
      height: rect.height,
    };
  }).filter((rect) => rect.width > 0 && rect.height > 0);
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
  const [expandedHostileSystemId, setExpandedHostileSystemId] = useState<number | null>(null);
  const [hostileDetailSystemId, setHostileDetailSystemId] = useState<number | null>(null);
  const [motionEnabled, setMotionEnabled] = useState(() => {
    const preference = typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
      ? undefined
      : window.matchMedia("(prefers-reduced-motion: reduce)");
    return !preference?.matches;
  });

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

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return undefined;
    }
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!preference) {
      return undefined;
    }
    const updatePreference = () => setMotionEnabled(!preference.matches);
    preference.addEventListener?.("change", updatePreference);
    return () => preference.removeEventListener?.("change", updatePreference);
  }, []);

  useEffect(() => {
    if (expandedHostileSystemId === null || graphData.nodes.some((node) =>
      node.kind === "hostile-summary" && node.systemId === expandedHostileSystemId
    )) {
      return;
    }
    setExpandedHostileSystemId(null);
    setHostileDetailSystemId(null);
  }, [expandedHostileSystemId, graphData.nodes]);

  const hasHostileCards = graphData.nodes.some(
    (node) => node.kind === "hostile-summary",
  );
  const fitPadding = hasHostileCards
    ? Math.min(200, Math.max(96, size.width * 0.2))
    : Math.min(96, Math.max(48, size.width * 0.15));
  const fitGraph = useCallback((duration = 650) => {
    graphRef.current?.zoomToFit(duration, fitPadding);
  }, [fitPadding]);
  const hasGraphData = graphData.nodes.length > 0;

  useEffect(() => {
    const syncAnimationState = () => {
      if (motionEnabled && hasGraphData && !document.hidden) {
        graphRef.current?.resumeAnimation();
      } else {
        graphRef.current?.pauseAnimation();
      }
    };

    syncAnimationState();
    document.addEventListener("visibilitychange", syncAnimationState);
    return () => {
      document.removeEventListener("visibilitychange", syncAnimationState);
      graphRef.current?.pauseAnimation();
    };
  }, [hasGraphData, motionEnabled]);

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
  const animatedNodePainter = useCallback((
    node: NodeObject<TacticalGraphNode>,
    context: CanvasRenderingContext2D,
    globalScale: number,
  ) => {
    drawNode(node, context, globalScale, motionEnabled ? animationTime() : 0);
  }, [motionEnabled]);
  const layoutHostileCards = useCallback((
    _context: CanvasRenderingContext2D,
    globalScale: number,
  ) => {
    const graph = graphRef.current;
    if (!graph) {
      return;
    }
    graphData.nodes.forEach((node) => {
      if (node.kind === "hostile-summary") {
        node.hostileCardHidden = node.systemId !== expandedHostileSystemId;
      }
    });
    if (expandedHostileSystemId === null) {
      return;
    }
    layoutHostileSummaryNodes(
      graphData.nodes.filter((node) =>
        node.kind !== "hostile-summary" || node.systemId === expandedHostileSystemId
      ),
      size.width,
      size.height,
      globalScale,
      {
        graphToScreen: (x, y) => graph.graph2ScreenCoords(x, y),
        screenToGraph: (x, y) => graph.screen2GraphCoords(x, y),
      },
      visibleOverlayRects(containerRef.current),
    );
  }, [expandedHostileSystemId, graphData.nodes, size.height, size.width]);
  const selectedHostileSummary = useMemo(() => graphData.nodes.find((node) =>
    node.kind === "hostile-summary" && node.systemId === hostileDetailSystemId,
  ), [graphData.nodes, hostileDetailSystemId]);
  const selectedHostiles = selectedHostileSummary?.hostileMembers || [];
  const unresolvedHostileCount = Math.max(
    0,
    (selectedHostileSummary?.hostileCount || 0) - selectedHostiles.length,
  );
  const closeHostileDetail = useCallback(() => setHostileDetailSystemId(null), []);
  const selectNode = useCallback((node: TacticalGraphNode) => {
    const systemId = typeof node.systemId === "number" ? node.systemId : null;
    onSelectSystem(systemId);
    if (systemId === null) {
      setExpandedHostileSystemId(null);
      setHostileDetailSystemId(null);
      return;
    }
    if (node.kind === "hostile-summary") {
      setHostileDetailSystemId(systemId);
      return;
    }
    setHostileDetailSystemId(null);
    if (node.hostileCount > 0) {
      setExpandedHostileSystemId((current) => current === systemId ? null : systemId);
    } else {
      setExpandedHostileSystemId(null);
    }
  }, [onSelectSystem]);

  return (
    <div
      className={`tactical-star-map ${motionEnabled ? "is-motion-enabled" : "reduce-motion"}`}
      data-testid="tactical-star-map"
      ref={containerRef}
    >
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
        autoPauseRedraw={!motionEnabled}
        cooldownTicks={0}
        enableNodeDrag={false}
        enablePanInteraction
        enablePointerInteraction
        enableZoomInteraction
        linkColor={linkColor}
        linkDirectionalParticles={motionEnabled ? linkParticleCount : 0}
        linkDirectionalParticleColor={linkParticleColor}
        linkDirectionalParticleSpeed={linkParticleSpeed}
        linkDirectionalParticleWidth={linkParticleWidth}
        linkCanvasObject={drawGateLink}
        linkCanvasObjectMode={() => "replace"}
        linkVisibility={(link) => {
          if (link.kind !== "hostile-intel") {
            return true;
          }
          return !graphNode(link.target)?.hostileCardHidden;
        }}
        linkWidth={(link) => {
          const source = graphNode(link.source);
          const target = graphNode(link.target);
          return source?.isSelected || target?.isSelected
            ? 1.9
            : 1.05;
        }}
        maxZoom={9}
        minZoom={0.12}
        nodeCanvasObject={animatedNodePainter}
        nodePointerAreaPaint={paintNodePointerArea}
        nodeVisibility={(node) => node.kind !== "hostile-summary" || !node.hostileCardHidden}
        onNodeClick={selectNode}
        onRenderFramePre={layoutHostileCards}
      />
      <Drawer
        className="hostile-detail-drawer"
        footer={null}
        title={selectedHostileSummary
          ? `${selectedHostileSummary.name} · 敌对详情`
          : "敌对详情"}
        visible={Boolean(selectedHostileSummary)}
        width={440}
        onCancel={closeHostileDetail}
      >
        <div className="hostile-detail-summary">
          <span>当前侦测</span>
          <strong>{selectedHostileSummary?.hostileCount || 0} 名敌对</strong>
          <Tag color="red">实时态势</Tag>
        </div>
        {selectedHostiles.length > 0 ? (
          <List
            className="hostile-detail-list"
            dataSource={selectedHostiles}
            render={(intel) => {
              const threat = THREAT_STYLE[intel.threatLevel];
              return (
                <List.Item key={`${intel.characterId ?? "unknown"}:${intel.name}`}>
                  <div className="hostile-detail-item">
                    <Avatar size={44}>
                      {portraitUrl(intel.characterId) ? (
                        <img
                          alt=""
                          src={portraitUrl(intel.characterId)}
                        />
                      ) : intel.name.slice(0, 1).toUpperCase()}
                    </Avatar>
                    <div className="hostile-detail-identity">
                      <div>
                        <strong>{intel.name}</strong>
                        <Tag color="red" size="small">
                          {intel.threatScore === null
                            ? threat.label
                            : `${threat.label} ${Math.round(intel.threatScore)}`}
                        </Tag>
                      </div>
                      <span title={intel.corporation}>军团 · {intel.corporation}</span>
                      <span title={intel.alliance}>联盟 · {intel.alliance}</span>
                    </div>
                  </div>
                </List.Item>
              );
            }}
          />
        ) : (
          <Empty description="已发现敌对信号，身份资料仍在解析" />
        )}
        {unresolvedHostileCount > 0 ? (
          <div className="hostile-detail-unresolved">
            另有 {unresolvedHostileCount} 名敌对身份待解析
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
