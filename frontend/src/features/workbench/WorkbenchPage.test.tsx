import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, forwardRef } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, test, vi } from "vitest";

import { WorkbenchPage } from "./WorkbenchPage";
import type { AlertItem, BootstrapPayload } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const bootstrap: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1",
  generated_at: "2026-07-02T12:00:00Z",
  map: {
    schema_version: "map_snapshot.v1",
    generated_at: "2026-07-02T12:00:00Z",
    systems: [
      {
        name: "0-UVHJ",
        system_id: 30003615,
        x: 100,
        y: 120,
        hostile_count: 1,
        report_count: 2,
      },
    ],
    links: [],
    summary: {
      system_count: 1,
      hostile_count: 1,
      report_count: 2,
      alert_count: 1,
    },
  },
  reports: [
    {
      id: "report-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      source: "channel",
      seen_at: "2026-07-02T12:00:00Z",
    },
  ],
  alerts: [
    {
      id: "alert-1",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      level: "high",
      score: 80,
      created_at: "2026-07-02T12:01:00Z",
    },
  ],
  clients: {
    count: 1,
    heartbeats: [],
    summary: {
      count: 1,
      online_count: 1,
      stale_count: 0,
    },
  },
  config: {
    schema_version: "scoring_config.v1",
    cooldown_seconds: 60,
  },
  esi: {
    enabled: true,
    authenticated: false,
  },
};

const apiMocks = vi.hoisted(() => {
  const close = vi.fn();
  return {
    close,
    onAlert: undefined as ((alert: AlertItem) => void) | undefined,
    connectAlerts: vi.fn((onAlert: (alert: AlertItem) => void) => {
      apiMocks.onAlert = onAlert;
      return { close };
    }),
    fetchBootstrap: vi.fn(async () => bootstrap),
  };
});

vi.mock("react-force-graph-2d", () => ({
  default: forwardRef(
    (
      { graphData }: { graphData: { nodes: unknown[]; links: unknown[] } },
      _ref,
    ) => (
      <div
        data-links={graphData.links.length}
        data-nodes={graphData.nodes.length}
        data-testid="force-graph"
      />
    ),
  ),
}));

vi.mock("./api", () => ({
  connectAlerts: apiMocks.connectAlerts,
  fetchBootstrap: apiMocks.fetchBootstrap,
}));

describe("WorkbenchPage", () => {
  test("renders the component-backed tactical workbench without reconnecting alerts on updates", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <WorkbenchPage />
        </QueryClientProvider>,
      );
    });

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container.querySelector('[data-testid="tactical-star-map"]')).toBeInTheDocument();
    expect(container.querySelector('[data-testid="force-graph"]')).toBeInTheDocument();
    expect(container.querySelector('[data-testid="threat-gauge"]')).not.toBeInTheDocument();
    expect(container.querySelector('[data-testid="echarts-gauge"]')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("当前威胁评分");
    expect(container).not.toHaveTextContent("威胁评分");
    expect(container).not.toHaveTextContent("ISK 损失风险");
    expect(container).not.toHaveTextContent("情报动向");
    expect(container.querySelector('[data-testid="observation-table"]')).toBeInTheDocument();
    expect(container).toHaveTextContent("Pilot One");
    expect(container).toHaveTextContent("预警频道");
    expect(container).toHaveTextContent("预警情报工作台");
    expect(container).toHaveTextContent("敌对飞行员观察列表");

    const mapButton = Array.from(container.querySelectorAll(".nav-panel button")).find(
      (button) => button.textContent?.includes("星图"),
    );
    expect(mapButton).toBeInTheDocument();
    await act(async () => {
      (mapButton as HTMLButtonElement).click();
    });
    expect(mapButton).toHaveClass("active");

    const viewModeButton = container.querySelector(
      '[aria-label="切换视图模式"]',
    ) as HTMLButtonElement;
    expect(viewModeButton).toHaveTextContent("安全态势");
    await act(async () => {
      viewModeButton.click();
    });
    expect(viewModeButton).toHaveTextContent("敌对活动");

    const listButton = container.querySelector(
      '[aria-label="列表视图"]',
    ) as HTMLButtonElement;
    await act(async () => {
      listButton.click();
    });
    expect(listButton).toHaveClass("active");
    expect(
      Array.from(container.querySelectorAll(".nav-panel button")).find((button) =>
        button.textContent?.includes("观察列表"),
      ),
    ).toHaveClass("active");

    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);
    await act(async () => {
      apiMocks.onAlert?.({
        id: "alert-2",
        system_name: "0-UVHJ",
        system_id: 30003615,
        names: ["Pilot Two"],
        level: "medium",
        score: 50,
        created_at: "2026-07-02T12:02:00Z",
      });
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container).toHaveTextContent("Pilot Two");
    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.unmount();
    });
    expect(apiMocks.close).toHaveBeenCalledTimes(1);
    container.remove();
  });
});
