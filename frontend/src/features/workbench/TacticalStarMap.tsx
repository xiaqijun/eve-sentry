import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type NodeObject,
} from "react-force-graph-2d";

import type {
  TacticalGraphData,
  TacticalGraphLink,
  TacticalGraphNode,
} from "./tacticalGraph";

interface TacticalStarMapProps {
  fitSignal?: number;
  graphData: TacticalGraphData;
  onSelectSystem: (systemId: number | null) => void;
}

function nodeColor(node: TacticalGraphNode): string {
  if (node.hostileCount > 0) {
    return "#ff4038";
  }
  if ((node.killCount ?? 0) > 0) {
    return "#ffb347";
  }
  if (node.channelIntelCount > 0 || node.observationCount > 0) {
    return "#17d7ff";
  }
  return "#a9d8ef";
}

function drawGateLink(
  link: TacticalGraphLink,
  context: CanvasRenderingContext2D,
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
  context.lineJoin = "round";

  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.strokeStyle = "rgba(0, 8, 12, 0.86)";
  context.lineWidth = isSelected ? 5.4 : 4.2;
  context.stroke();

  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.shadowColor = isHot ? "#ff4b40" : isLossHot ? "#ffb347" : "#1ccfff";
  context.shadowBlur = isHot || isSelected ? 14 : 7;
  context.strokeStyle = isHot
    ? "rgba(255, 87, 70, 0.9)"
    : isLossHot
      ? "rgba(255, 179, 71, 0.76)"
    : isSelected
      ? "rgba(114, 232, 255, 0.96)"
      : "rgba(64, 207, 255, 0.72)";
  context.lineWidth = isSelected ? 2.4 : 1.65;
  context.stroke();

  context.shadowBlur = 0;
  context.setLineDash([7, 9]);
  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.strokeStyle = isHot
    ? "rgba(255, 191, 93, 0.72)"
    : "rgba(177, 244, 255, 0.28)";
  context.lineWidth = 0.8;
  context.stroke();
  context.restore();
}

function drawNode(
  node: NodeObject<TacticalGraphNode>,
  context: CanvasRenderingContext2D,
  globalScale: number,
): void {
  const label = node.name;
  const color = nodeColor(node);
  const lossCount = Math.max(0, node.killCount ?? 0);
  const hasHud = node.hostileCount > 0 || lossCount > 0;
  const hasChannelIntel = node.channelIntelCount > 0;
  const radius = node.isSelected ? 6.5 : hasHud ? 5.8 : 4.4;
  const fontSize = Math.max(7, 10 / globalScale);
  const x = Number(node.x || 0);
  const y = Number(node.y || 0);

  context.save();
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fillStyle = "rgba(2, 10, 14, 0.92)";
  context.fill();
  context.lineWidth = node.isSelected ? 2.2 : 1.6;
  context.strokeStyle = color;
  context.shadowColor = color;
  context.shadowBlur = hasHud || node.isSelected ? 18 : 9;
  context.stroke();

  if (hasHud || node.isSelected) {
    context.shadowBlur = 0;
    context.strokeStyle =
      node.hostileCount > 0
        ? "rgba(255, 64, 56, 0.34)"
        : lossCount > 0
          ? "rgba(255, 179, 71, 0.34)"
          : "rgba(255, 255, 255, 0.42)";
    context.lineWidth = 1;
    context.beginPath();
    context.arc(x, y, radius + 10, 0, Math.PI * 2);
    context.stroke();
    context.beginPath();
    context.arc(x, y, radius + 18, 0, Math.PI * 2);
    context.stroke();
  }

  if (node.monitorCount > 0) {
    const badgeSize = Math.max(4, 6 / globalScale);
    const badgeX = x + radius + 5;
    const badgeY = y - radius - badgeSize / 2;
    const monitorOnline = node.monitorOnlineCount > 0;
    context.shadowBlur = monitorOnline ? 12 : 8;
    context.shadowColor = monitorOnline ? "#20e879" : "#ffae32";
    context.fillStyle = monitorOnline
      ? "rgba(32, 232, 121, 0.9)"
      : "rgba(255, 174, 50, 0.82)";
    context.beginPath();
    context.arc(badgeX, badgeY, badgeSize, 0, Math.PI * 2);
    context.fill();
    context.shadowBlur = 0;
    context.strokeStyle = monitorOnline
      ? "rgba(199, 255, 222, 0.95)"
      : "rgba(255, 229, 179, 0.88)";
    context.lineWidth = Math.max(0.8, 1 / globalScale);
    context.stroke();
  }

  if (hasChannelIntel && !hasHud) {
    const intelRadius = Math.max(3.2, 4.8 / globalScale);
    context.shadowBlur = 12;
    context.shadowColor = "#17d7ff";
    context.strokeStyle = "rgba(23, 215, 255, 0.82)";
    context.lineWidth = Math.max(0.8, 1 / globalScale);
    context.beginPath();
    context.moveTo(x, y - radius - intelRadius - 3);
    context.lineTo(x + intelRadius, y - radius - 3);
    context.lineTo(x, y - radius + intelRadius - 3);
    context.lineTo(x - intelRadius, y - radius - 3);
    context.closePath();
    context.stroke();
  }

  context.shadowBlur = 0;
  context.font = `700 ${fontSize}px "Segoe UI", sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "top";
  context.fillStyle = "#eaf8ff";
  context.fillText(label, x, y + radius + 3);
  if (hasHud) {
    const hudFontSize = Math.max(6, 8 / globalScale);
    const paddingX = Math.max(4, 5 / globalScale);
    const paddingY = Math.max(2, 3 / globalScale);
    const gap = Math.max(3, 4 / globalScale);
    const hudY = y + radius + 5 + fontSize;
    const segments = [
      { color: "#ff6a5f", text: "敌:" },
      { color: "#ffffff", text: String(node.hostileCount) },
      { color: "#ffc266", text: "损:" },
      { color: "#ffffff", text: String(lossCount) },
    ];

    context.font = `700 ${hudFontSize}px "Cascadia Mono", "Consolas", "Segoe UI", monospace`;
    const widths = segments.map((segment) => context.measureText(segment.text).width);
    const textWidth = widths.reduce((sum, width) => sum + width, 0) + gap * 3;
    const hudWidth = textWidth + paddingX * 2;
    const hudHeight = hudFontSize + paddingY * 2;
    const hudX = x - hudWidth / 2;

    context.shadowColor = node.hostileCount > 0 ? "#ff4038" : "#ffb347";
    context.shadowBlur = 12;
    context.fillStyle =
      node.hostileCount > 0
        ? "rgba(24, 5, 5, 0.82)"
        : "rgba(24, 16, 4, 0.78)";
    context.fillRect(hudX, hudY, hudWidth, hudHeight);
    context.shadowBlur = 0;
    context.strokeStyle =
      node.hostileCount > 0
        ? "rgba(255, 90, 80, 0.62)"
        : "rgba(255, 190, 92, 0.62)";
    context.lineWidth = Math.max(0.8, 1 / globalScale);
    context.strokeRect(hudX, hudY, hudWidth, hudHeight);

    context.textAlign = "left";
    context.textBaseline = "top";
    let cursor = hudX + paddingX;
    segments.forEach((segment, index) => {
      context.fillStyle = segment.color;
      context.fillText(segment.text, cursor, hudY + paddingY);
      cursor += widths[index] + (index === 1 ? gap * 2 : gap);
    });
  }
  context.restore();
}

function paintNodePointerArea(
  node: NodeObject<TacticalGraphNode>,
  color: string,
  context: CanvasRenderingContext2D,
): void {
  context.fillStyle = color;
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
        width: Math.max(640, entry.contentRect.width),
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const fitGraph = useCallback((duration = 650) => {
    graphRef.current?.zoomToFit(duration, 96);
  }, []);

  useEffect(() => {
    const quickFit = window.setTimeout(() => fitGraph(0), 80);
    const settledFit = window.setTimeout(() => fitGraph(650), 360);
    return () => {
      window.clearTimeout(quickFit);
      window.clearTimeout(settledFit);
    };
  }, [fitGraph, fitSignal, graphData, size.height, size.width]);

  const linkColor = useMemo(() => "rgba(39, 201, 255, 0.34)", []);

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
          (graphNode(link.source)?.hostileCount || 0) > 0 ? 1 : 0
        }
        linkDirectionalParticleColor={() => "rgba(255, 74, 64, 0.82)"}
        linkDirectionalParticleSpeed={0.004}
        linkDirectionalParticleWidth={1.4}
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
        minZoom={0.35}
        nodeCanvasObject={drawNode}
        nodePointerAreaPaint={paintNodePointerArea}
        onNodeClick={(node) =>
          onSelectSystem(typeof node.systemId === "number" ? node.systemId : null)
        }
      />
    </div>
  );
}
