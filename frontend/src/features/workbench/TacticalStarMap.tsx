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
    return "#ef5b52";
  }
  if ((node.killCount ?? 0) > 0) {
    return "#dca548";
  }
  if (node.channelIntelCount > 0 || node.observationCount > 0) {
    return "#55a7c7";
  }
  if (node.monitorOnlineCount > 0) {
    return "#55bd92";
  }
  return "#6d8089";
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
  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.strokeStyle = isHot
    ? "rgba(239, 91, 82, 0.72)"
    : isLossHot
      ? "rgba(220, 165, 72, 0.58)"
      : isSelected
        ? "rgba(115, 191, 164, 0.74)"
        : "rgba(105, 132, 143, 0.30)";
  context.lineWidth = isSelected ? 2.2 : isHot || isLossHot ? 1.5 : 1;
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
      ? "rgba(85, 189, 146, 0.10)"
      : "rgba(239, 91, 82, 0.09)";
    context.fill();
    context.strokeStyle = node.isSelected
      ? "rgba(85, 189, 146, 0.38)"
      : "rgba(239, 91, 82, 0.28)";
    context.lineWidth = 1;
    context.stroke();
  }

  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
  context.lineWidth = node.isSelected ? 2 : 1.2;
  context.strokeStyle = node.isSelected ? "#e8fff6" : "rgba(255,255,255,0.72)";
  context.stroke();

  context.font = `600 ${fontSize}px "Segoe UI", "Microsoft YaHei", sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "top";
  context.fillStyle = node.isSelected || isActive ? "#eef6f3" : "#9fadb1";
  context.fillText(label, x, y + radius + 3);

  if (node.hostileCount > 0 || lossCount > 0) {
    const badgeText = node.hostileCount > 0 ? String(node.hostileCount) : String(lossCount);
    const badgeRadius = Math.max(4.2, 5.2 / globalScale);
    const badgeX = x + radius + 2;
    const badgeY = y - radius - 2;
    context.beginPath();
    context.arc(badgeX, badgeY, badgeRadius, 0, Math.PI * 2);
    context.fillStyle = node.hostileCount > 0 ? "#b92f28" : "#9b6e1d";
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

  const linkColor = useMemo(() => "rgba(105, 132, 143, 0.30)", []);

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
        linkDirectionalParticleColor={() => "rgba(239, 91, 82, 0.86)"}
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
