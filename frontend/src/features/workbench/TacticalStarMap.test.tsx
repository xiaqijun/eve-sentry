import "@testing-library/jest-dom/vitest";
import { act, forwardRef, useImperativeHandle } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, test, vi } from "vitest";

import { TacticalStarMap } from "./TacticalStarMap";
import type { TacticalGraphData } from "./tacticalGraph";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const forceGraphMock = vi.hoisted(() => ({
  latestProps: null as Record<string, unknown> | null,
  pauseAnimation: vi.fn(),
  resumeAnimation: vi.fn(),
  zoomToFit: vi.fn(),
}));

vi.mock("react-force-graph-2d", () => ({
  default: forwardRef((props: Record<string, unknown>, ref) => {
    useImperativeHandle(ref, () => ({
      pauseAnimation: forceGraphMock.pauseAnimation,
      resumeAnimation: forceGraphMock.resumeAnimation,
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
      channelIntelCount: 0,
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
      channelIntelCount: 0,
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

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("TacticalStarMap", () => {
  test("preserves the user zoom when refreshed graph data arrives", async () => {
    vi.useFakeTimers();
    forceGraphMock.zoomToFit.mockClear();
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap graphData={graphData} onSelectSystem={() => {}} />,
      );
    });
    act(() => vi.advanceTimersByTime(400));

    expect(forceGraphMock.zoomToFit).toHaveBeenCalledTimes(2);

    const refreshedGraphData: TacticalGraphData = {
      ...graphData,
      nodes: graphData.nodes.map((node) => ({
        ...node,
        hostileCount: node.hostileCount + 1,
      })),
    };
    await act(async () => {
      root.render(
        <TacticalStarMap
          graphData={refreshedGraphData}
          onSelectSystem={() => {}}
        />,
      );
    });
    act(() => vi.advanceTimersByTime(400));

    expect(forceGraphMock.zoomToFit).toHaveBeenCalledTimes(2);

    await act(async () => {
      root.render(
        <TacticalStarMap
          fitSignal={1}
          graphData={refreshedGraphData}
          onSelectSystem={() => {}}
        />,
      );
    });
    act(() => vi.advanceTimersByTime(400));

    expect(forceGraphMock.zoomToFit).toHaveBeenCalledTimes(4);

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

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

  test("animates semantic activity and pauses rendering when motion is reduced", async () => {
    forceGraphMock.pauseAnimation.mockClear();
    forceGraphMock.resumeAnimation.mockClear();
    const matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as MediaQueryList);
    vi.stubGlobal("matchMedia", matchMedia);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap graphData={graphData} onSelectSystem={() => {}} />,
      );
    });

    expect(forceGraphMock.resumeAnimation).toHaveBeenCalled();
    expect(forceGraphMock.latestProps?.autoPauseRedraw).toBe(false);
    const particleCount = forceGraphMock.latestProps?.linkDirectionalParticles as Function;
    const particleColor = forceGraphMock.latestProps?.linkDirectionalParticleColor as Function;
    const activeLink = {
      ...graphData.links[0],
      source: graphData.nodes[0],
      target: graphData.nodes[1],
    };
    expect(particleCount(activeLink)).toBe(2);
    expect(particleColor(activeLink)).toBe("rgba(214, 69, 61, 0.82)");

    matchMedia.mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as MediaQueryList);
    await act(async () => {
      root.unmount();
    });

    const reducedMotionRoot = createRoot(container);
    await act(async () => {
      reducedMotionRoot.render(
        <TacticalStarMap graphData={graphData} onSelectSystem={() => {}} />,
      );
    });
    expect(forceGraphMock.pauseAnimation).toHaveBeenCalled();
    expect(forceGraphMock.latestProps?.autoPauseRedraw).toBe(true);
    expect(forceGraphMock.latestProps?.linkDirectionalParticles).toBe(0);
    expect(container.querySelector(".reduce-motion")).toBeInTheDocument();

    await act(async () => {
      reducedMotionRoot.unmount();
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

  test("draws system labels and compact threat badges without legacy HUD text", async () => {
    const monitoredGraphData: TacticalGraphData = {
      ...graphData,
      nodes: graphData.nodes.map((node) =>
        node.id === "0-UVHJ"
          ? {
              ...node,
              hostileCount: 3,
              killCount: 1,
              hasAlerts: true,
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
    const arc = vi.fn();
    const strokeColors: string[] = [];
    let context: CanvasRenderingContext2D;
    const stroke = vi.fn(() => strokeColors.push(String(context.strokeStyle)));
    const measureText = vi.fn((text: string) => ({ width: text.length * 6 }));
    context = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      arc,
      fill: vi.fn(),
      stroke,
      fillRect: vi.fn(),
      strokeRect: vi.fn(),
      fillText,
      measureText,
    } as unknown as CanvasRenderingContext2D;
    const drawNode = forceGraphMock.latestProps?.nodeCanvasObject;

    expect(drawNode).toEqual(expect.any(Function));
    (drawNode as Function)(monitoredGraphData.nodes[0], context, 1);

    expect(fillText).not.toHaveBeenCalledWith(
      "在线 1",
      expect.any(Number),
      expect.any(Number),
    );
    expect(fillText).not.toHaveBeenCalledWith(
      "实时目标 3 记录 0 击杀 1",
      expect.any(Number),
      expect.any(Number),
    );
    expect(fillText).toHaveBeenCalledWith("0-UVHJ", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("3", expect.any(Number), expect.any(Number));
    expect(fillText).not.toHaveBeenCalledWith("敌:", expect.any(Number), expect.any(Number));
    expect(fillText).not.toHaveBeenCalledWith("损:", expect.any(Number), expect.any(Number));
    expect(fillText).not.toHaveBeenCalledWith("1", expect.any(Number), expect.any(Number));
    expect(strokeColors).toContain("rgba(214, 69, 61, 0.24)");
    expect(arc.mock.calls.length).toBeGreaterThanOrEqual(5);

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  test("draws hostile identity cards with portraits, risk badges, and organization rows", async () => {
    const hostileCard = {
      ...graphData.nodes[1],
      id: "hostile:NCG-PW:id:90000001",
      name: "Pilot One",
      kind: "hostile-card" as const,
      x: 304,
      y: 150,
      fx: 304,
      fy: 150,
      hostileCount: 0,
      threatLevel: "high" as const,
      threatScore: 88,
      hostileIntel: {
        characterId: 90000001,
        name: "Pilot One",
        corporation: "Red Horizon",
        alliance: "Northern Threat",
        threatLevel: "high" as const,
        threatScore: 88,
      },
    };
    const hostileGraphData: TacticalGraphData = {
      nodes: [...graphData.nodes, hostileCard],
      links: [
        ...graphData.links,
        {
          source: "NCG-PW",
          target: hostileCard.id,
          kind: "hostile-intel",
        },
      ],
    };
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <TacticalStarMap
          graphData={hostileGraphData}
          onSelectSystem={() => {}}
        />,
      );
    });

    const fillText = vi.fn();
    const fillRect = vi.fn();
    const strokeRect = vi.fn();
    const lineTo = vi.fn();
    const context = {
      save: vi.fn(),
      restore: vi.fn(),
      translate: vi.fn(),
      scale: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo,
      arc: vi.fn(),
      fill: vi.fn(),
      stroke: vi.fn(),
      fillRect,
      strokeRect,
      fillText,
      measureText: vi.fn((text: string) => ({ width: text.length * 5 })),
      setLineDash: vi.fn(),
    } as unknown as CanvasRenderingContext2D;
    const drawNode = forceGraphMock.latestProps?.nodeCanvasObject;
    const drawLink = forceGraphMock.latestProps?.linkCanvasObject;

    expect(drawNode).toEqual(expect.any(Function));
    (drawNode as Function)(hostileCard, context, 1);
    expect(fillText).toHaveBeenCalledWith("Pilot One", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("高危 88", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("军", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("联", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("Red Horizon", expect.any(Number), expect.any(Number));
    expect(fillText).toHaveBeenCalledWith("Northern Threat", expect.any(Number), expect.any(Number));
    expect(fillRect.mock.calls.length).toBeGreaterThanOrEqual(8);
    expect(strokeRect).toHaveBeenCalled();

    (drawLink as Function)(
      {
        ...hostileGraphData.links[1],
        source: graphData.nodes[1],
        target: hostileCard,
      },
      context,
    );
    expect(lineTo).toHaveBeenCalledTimes(3);

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });
});
