import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "./DashboardPage";
import type { BootstrapPayload } from "../workbench/types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const apiMocks = vi.hoisted(() => ({
  fetchBootstrap: vi.fn(),
  fetchHostileAlertHistory: vi.fn(),
}));

vi.mock("../workbench/api", () => ({
  fetchBootstrap: apiMocks.fetchBootstrap,
}));
vi.mock("../reports/api", () => ({
  fetchHostileAlertHistory: apiMocks.fetchHostileAlertHistory,
}));
vi.mock("../../components/EveChart", () => ({
  EveChart: ({ className }: { className?: string }) => (
    <div className={className} data-testid="eve-chart" />
  ),
}));

describe("DashboardPage", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.clearAllMocks();
  });

  it("summarizes live systems and verified hostile intelligence", async () => {
    const now = Date.now();
    const bootstrap: BootstrapPayload = {
      schema_version: "intel_bootstrap.v1",
      generated_at: new Date(now).toISOString(),
      map: {
        schema_version: "map_snapshot.v1",
        generated_at: new Date(now).toISOString(),
        systems: [{
          name: "S-KSWL",
          system_id: 30003615,
          x: 100,
          y: 120,
          hostile_count: 1,
          kill_count: 2,
        }],
        links: [],
        summary: {},
      },
      reports: [],
      alerts: [{
        id: "live-alert",
        system_name: "S-KSWL",
        system_id: 30003615,
        names: ["Alice", "Bob", "Carol", "Dave", "Eve Long Name"],
        verified_characters: [
          {
            character_id: 101,
            name: "Alice",
            zkill: { danger_ratio: 86, ships_destroyed: 420, ships_lost: 23 },
          },
          { character_id: 102, name: "Bob" },
          { character_id: 103, name: "Carol" },
          { character_id: 104, name: "Dave" },
          { character_id: 105, name: "Eve Long Name" },
        ],
        classification: "red",
        level: "high",
        created_at: new Date(now - 5 * 60 * 1000).toISOString(),
      }],
      clients: {
        count: 1,
        heartbeats: [{
          client_id: "monitor-1",
          label: "监控客户端 · 不应被省略的完整名称",
          client_type: "alert_client",
          online: true,
          system_id: 30003615,
          details: { monitoring: true },
        }],
        summary: { count: 1, online_count: 1, stale_count: 0 },
      },
      config: null,
      esi: { enabled: true, authenticated: true },
    };
    apiMocks.fetchBootstrap.mockResolvedValue(bootstrap);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <DashboardPage />
        </QueryClientProvider>,
      );
    });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container).toHaveTextContent("工作台");
    expect(container).toHaveTextContent("在线监控星系");
    expect(container).toHaveTextContent("当前敌对人数");
    expect(container).toHaveTextContent("当前告警事件");
    expect(container).toHaveTextContent("异常客户端");
    expect(container).toHaveTextContent("当前敌对星系");
    expect(container).toHaveTextContent("S-KSWL");
    expect(container).toHaveTextContent("威胁度 86");
    expect(container).toHaveTextContent("最新告警事件");
    expect(container).toHaveTextContent("高");
    expect(container).toHaveTextContent("监控覆盖");
    expect(container).toHaveTextContent("Alice");
    expect(container).toHaveTextContent("Eve Long Name");
    expect(container).toHaveTextContent("监控客户端 · 不应被省略的完整名称");
    expect(container.querySelectorAll('[data-testid="eve-chart"]')).toHaveLength(0);
    expect(apiMocks.fetchHostileAlertHistory).not.toHaveBeenCalled();

    await act(async () => root.unmount());
  });
});
