import "@testing-library/jest-dom/vitest";
import { act, forwardRef, useImperativeHandle } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, test, vi } from "vitest";

import { TacticalStarMap } from "./TacticalStarMap";
import type { TacticalGraphData } from "./tacticalGraph";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const forceGraphMock = vi.hoisted(() => ({
  latestProps: null as Record<string, unknown> | null,
  zoomToFit: vi.fn(),
}));

vi.mock("react-force-graph-2d", () => ({
  default: forwardRef((props: Record<string, unknown>, ref) => {
    useImperativeHandle(ref, () => ({
      zoomToFit: forceGraphMock.zoomToFit,
    }));
    forceGraphMock.latestProps = props;
    return <div data-testid="force-graph" />;
  }),
}));

const graphData: TacticalGraphData = {
  nodes: [
    {
      id: "0-UVHJ",
      name: "0-UVHJ",
      systemId: 30003615,
      x: 100,
      y: 120,
      fx: 100,
      fy: 120,
      security: -0.1,
      hostileCount: 0,
      reportCount: 0,
      observationCount: 0,
      killCount: null,
      monitorCount: 0,
      monitorOnlineCount: 0,
      monitorLabels: [],
      hasAlerts: false,
      isSelected: false,
      threatLevel: "unknown",
      threatScore: null,
    },
    {
      id: "NCG-PW",
      name: "NCG-PW",
      systemId: 30003616,
      x: 180,
      y: 150,
      fx: 180,
      fy: 150,
      security: -0.3,
      hostileCount: 1,
      reportCount: 1,
      observationCount: 1,
      killCount: null,
      monitorCount: 0,
      monitorOnlineCount: 0,
      monitorLabels: [],
      hasAlerts: true,
      isSelected: false,
      threatLevel: "high",
      threatScore: 80,
    },
  ],
  links: [{ source: "0-UVHJ", target: "NCG-PW" }],
};

describe("TacticalStarMap", () => {
  test("uses a custom high-contrast link painter for gate connections", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap graphData={graphData} onSelectSystem={() => {}} />,
      );
    });

    expect(forceGraphMock.latestProps?.linkCanvasObject).toEqual(
      expect.any(Function),
    );
    expect(forceGraphMock.latestProps?.linkCanvasObjectMode).toEqual(
      expect.any(Function),
    );

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  test("does not draw an extra canvas background layer over the HUD image", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap graphData={graphData} onSelectSystem={() => {}} />,
      );
    });

    expect(forceGraphMock.latestProps?.onRenderFramePre).toBeUndefined();

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  test("shows a readable empty state when there are no live hostile targets", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap
          graphData={{ links: [], nodes: [] }}
          onSelectSystem={() => {}}
        />,
      );
    });

    const emptyState = container.querySelector(".tactical-star-map-empty");
    expect(emptyState).toHaveTextContent("暂无实时敌对目标");
    expect(container.querySelector('[data-testid="force-graph"]')).toBeInTheDocument();

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  test("draws readable status labels for systems with deployed clients", async () => {
    const monitoredGraphData: TacticalGraphData = {
      ...graphData,
      nodes: graphData.nodes.map((node) =>
        node.id === "0-UVHJ"
          ? {
              ...node,
              monitorCount: 1,
              monitorOnlineCount: 1,
              monitorLabels: ["Tenal OCR Monitor"],
            }
          : node,
      ),
    };
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap
          graphData={monitoredGraphData}
          onSelectSystem={() => {}}
        />,
      );
    });

    const fillText = vi.fn();
    const context = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      stroke: vi.fn(),
      fillRect: vi.fn(),
      strokeRect: vi.fn(),
      fillText,
    } as unknown as CanvasRenderingContext2D;
    const drawNode = forceGraphMock.latestProps?.nodeCanvasObject;

    expect(drawNode).toEqual(expect.any(Function));
    (drawNode as Function)(monitoredGraphData.nodes[0], context, 1);

    expect(fillText).toHaveBeenCalledWith(
      "在线 1",
      expect.any(Number),
      expect.any(Number),
    );
    expect(fillText).toHaveBeenCalledWith(
      "实时目标 0 记录 0 击杀 -",
      expect.any(Number),
      expect.any(Number),
    );

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });
});
