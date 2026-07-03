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
  if (node.hasAlerts) {
    return "#ff4038";
  }
  if (node.hostileCount > 0) {
    return "#ff9f16";
  }
  if (node.observationCount > 0) {
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

  const isHot = source.hasAlerts || target.hasAlerts;
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
  context.shadowColor = isHot ? "#ff4b40" : "#1ccfff";
  context.shadowBlur = isHot || isSelected ? 14 : 7;
  context.strokeStyle = isHot
    ? "rgba(255, 87, 70, 0.9)"
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
  const radius = node.isSelected ? 6.5 : node.hasAlerts ? 5.8 : 4.6;
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
  context.shadowBlur = node.hasAlerts || node.isSelected ? 18 : 9;
  context.stroke();

  if (node.hasAlerts || node.isSelected) {
    context.shadowBlur = 0;
    context.strokeStyle = node.hasAlerts
      ? "rgba(255, 64, 56, 0.34)"
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
    const badgeSize = Math.max(7, 10 / globalScale);
    const badgeX = x + radius + 5;
    const badgeY = y - radius - badgeSize;
    const monitorOnline = node.monitorOnlineCount > 0;
    context.shadowBlur = monitorOnline ? 12 : 8;
    context.shadowColor = monitorOnline ? "#20e879" : "#ffae32";
    context.fillStyle = monitorOnline
      ? "rgba(32, 232, 121, 0.9)"
      : "rgba(255, 174, 50, 0.82)";
    context.fillRect(badgeX, badgeY, badgeSize, badgeSize);
    context.shadowBlur = 0;
    context.strokeStyle = monitorOnline
      ? "rgba(199, 255, 222, 0.95)"
      : "rgba(255, 229, 179, 0.88)";
    context.lineWidth = Math.max(0.8, 1 / globalScale);
    context.strokeRect(badgeX, badgeY, badgeSize, badgeSize);
    context.fillStyle = monitorOnline ? "#b8ffd2" : "#ffdca3";
    context.font = `700 ${Math.max(6, 8 / globalScale)}px "Segoe UI", sans-serif`;
    context.textAlign = "left";
    context.textBaseline = "middle";
    context.fillText(`监${node.monitorCount}`, badgeX + badgeSize + 3, badgeY + badgeSize / 2);
  }

  context.shadowBlur = 0;
  context.font = `700 ${fontSize}px "Segoe UI", sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "top";
  context.fillStyle = "#eaf8ff";
  context.fillText(label, x, y + radius + 3);
  context.fillStyle = node.hasAlerts ? "#ff5048" : "#9fb7c4";
  context.font = `600 ${Math.max(6, 8 / globalScale)}px "Segoe UI", sans-serif`;
  context.fillText(
    `敌 ${node.hostileCount} 观 ${node.observationCount} 杀 ${node.killCount ?? "-"}`,
    x,
    y + radius + 3 + fontSize,
  );
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
          graphNode(link.source)?.hasAlerts ? 1 : 0
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
