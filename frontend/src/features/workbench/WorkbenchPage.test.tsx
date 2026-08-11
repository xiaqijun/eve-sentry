import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, forwardRef } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { mergeBootstrapStreamUpdate, WorkbenchPage } from "./WorkbenchPage";
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
        region: "Tenal",
        hostile_count: 1,
        report_count: 2,
      },
    ],
    links: [],
    summary: {
      system_count: 1,
      hostile_count: 99,
      report_count: 9,
      alert_count: 7,
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
      verified_characters: [{ character_id: 101, name: "Pilot One" }],
      classification: "red",
      level: "high",
      score: 80,
      created_at: "2026-07-02T12:01:00Z",
    },
    {
      id: "alert-duplicate",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot One"],
      verified_characters: [{ character_id: 101, name: "Pilot One" }],
      classification: "red",
      level: "medium",
      created_at: "2026-07-02T12:00:30Z",
    },
    {
      id: "alert-unverified",
      system_name: "1DQ1-A",
      system_id: 30004759,
      names: ["OCR Noise"],
      verified_characters: [{ character_id: 0, name: "OCR Noise" }],
      classification: "red",
      level: "critical",
      created_at: "2026-07-02T12:00:20Z",
    },
    {
      id: "alert-friendly",
      system_name: "F-NMX6",
      system_id: 30004758,
      names: ["Friendly Pilot"],
      verified_characters: [{ character_id: 202, name: "Friendly Pilot" }],
      classification: "white",
      level: "low",
      created_at: "2026-07-02T12:00:10Z",
    },
  ],
  clients: {
    count: 1,
    heartbeats: [{
      client_id: "monitor-1",
      client_type: "alert_client",
      online: true,
      system_id: 30003615,
      details: { monitoring: true },
    }],
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
    session: false,
    config: {
      client_id_configured: false,
      token_file_present: false,
      token_storage: "plain",
      scopes: ["esi-location.read_location.v1"],
    },
  },
};

const apiMocks = vi.hoisted(() => {
  const close = vi.fn();
  return {
    close,
    onAlert: undefined as ((alert: AlertItem) => void) | undefined,
    onBootstrap: undefined as ((bootstrap: BootstrapPayload) => void) | undefined,
    connectAlerts: vi.fn((
      onAlert: (alert: AlertItem) => void,
      _since?: string,
      _onError?: () => void,
      onBootstrap?: (bootstrap: BootstrapPayload) => void,
    ) => {
      apiMocks.onAlert = onAlert;
      apiMocks.onBootstrap = onBootstrap;
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
  test("preserves the full map when a compact stream update omits links", () => {
    const current = {
      ...bootstrap,
      map: {
        ...bootstrap.map,
        links: [{ from: "0-UVHJ", to: "NCG-PW" }],
      },
    };

    const merged = mergeBootstrapStreamUpdate(current, {
      generated_at: "2026-07-02T12:02:00Z",
      map: {
        systems: [{
          name: "0-UVHJ",
          system_id: 30003615,
          x: 100,
          y: 120,
          hostile_count: 2,
        }],
        summary: { hostile_count: 2 },
      },
      alerts: [],
      active_intel: [],
    });

    expect(merged.map.links).toEqual(current.map.links);
    expect(merged.map.systems[0].hostile_count).toBe(2);
    expect(merged.reports).toEqual(current.reports);
    expect(merged.config).toEqual(current.config);
  });

  test("merges compact hostile systems without dropping the rest of the map", () => {
    const current = {
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: [
          ...bootstrap.map.systems,
          {
            name: "NCG-PW",
            system_id: 30003616,
            x: 160,
            y: 180,
            hostile_count: 3,
          },
        ],
      },
    };

    const merged = mergeBootstrapStreamUpdate(current, {
      map: {
        systems: [{ name: "0-UVHJ", hostile_count: 2 }],
        summary: { alert_count: 1 },
      },
      active_intel: [],
      alerts: [],
    });

    expect(merged.map.systems).toHaveLength(2);
    expect(merged.map.systems.map((system) => system.hostile_count)).toEqual([2, 0]);
    expect(merged.map.systems.map((system) => system.name)).toEqual(["0-UVHJ", "NCG-PW"]);
  });

  test("keeps gate links when a compact update includes an empty links array", () => {
    const current = {
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: [
          ...bootstrap.map.systems,
          {
            name: "NCG-PW",
            system_id: 30003616,
            x: 160,
            y: 180,
          },
        ],
        links: [{ from: "0-UVHJ", to: "NCG-PW" }],
      },
    };

    const merged = mergeBootstrapStreamUpdate(current, {
      map: {
        systems: [{ name: "0-UVHJ", hostile_count: 1 }],
        links: [],
      },
    });

    expect(merged.map.links).toEqual([{ from: "0-UVHJ", to: "NCG-PW" }]);
  });

  test("keeps gate links when an empty hostile update clears counts", () => {
    const current = {
      ...bootstrap,
      map: {
        ...bootstrap.map,
        systems: bootstrap.map.systems.map((system) => ({
          ...system,
          hostile_count: 2,
        })),
        links: [{ from: "0-UVHJ", to: "NCG-PW" }],
      },
    };

    const merged = mergeBootstrapStreamUpdate(current, {
      map: { systems: [], links: [] },
    });

    expect(merged.map.systems.map((system) => system.hostile_count)).toEqual([0]);
    expect(merged.map.links).toEqual([{ from: "0-UVHJ", to: "NCG-PW" }]);
  });

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 1280 });
    apiMocks.onAlert = undefined;
    apiMocks.onBootstrap = undefined;
    apiMocks.fetchBootstrap.mockImplementation(async () => bootstrap);
  });

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
    expect(container.querySelector('[data-testid="observation-table"]')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("预警情报工作台");
    const situationStats = container.querySelector('[aria-label="态势统计"]');
    expect(situationStats).toBeInTheDocument();
    expect(situationStats).toHaveTextContent("在线预警节点1");
    expect(situationStats).toHaveTextContent("当前有敌星系1");
    expect(situationStats).toHaveTextContent("当前敌对人数1");
    expect(situationStats).toHaveTextContent("更新时间");
    expect(container).not.toHaveTextContent("区域态势");
    expect(container.querySelector(".sector-panel")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("敌对飞行员观察列表");
    expect(container).not.toHaveTextContent("ESI 状态");
    expect(container.querySelector(".esi-login-button")).not.toBeInTheDocument();

    expect(container.querySelector('[aria-label="情报详情"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="右侧面板切换"]')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("情报面板");
    expect(container.querySelector(".quick-icons")).not.toBeInTheDocument();
    expect(container.querySelector(".sector-preview")).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="星图工作区"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换区域"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换视图模式"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换视图"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="列表视图"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="设置"]')).not.toBeInTheDocument();
    expect(container.querySelector(".map-toolbar")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("视图模式：");
    expect(container).not.toHaveTextContent("视图：星图");
    expect(container.querySelector('[aria-label="Fit 星图"]')).toBeInTheDocument();
    const mapLegend = container.querySelector(".map-legend");
    expect(mapLegend).toHaveTextContent("节点状态");
    expect(mapLegend).toHaveTextContent("在线监控");
    expect(mapLegend).toHaveTextContent("实时敌对");
    expect(mapLegend).toHaveTextContent("活跃情报");
    expect(mapLegend).toHaveTextContent("近 1 小时损失");
    expect(mapLegend).toHaveTextContent("当前选中");
    expect(mapLegend).toHaveTextContent("数字徽标表示敌对人数或损失数");
    expect(mapLegend?.querySelectorAll("span")).toHaveLength(5);
    expect(mapLegend).not.toHaveTextContent("高安全区");
    expect(mapLegend).not.toHaveTextContent("低安全区");
    expect(mapLegend).not.toHaveTextContent("跃迁通道");
    expect(mapLegend).not.toHaveTextContent("跃迁抑制");
    expect(container.querySelector(".star-event-bar")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("最新事件");
    expect(container).not.toHaveTextContent("数据状态：");
    expect(container).not.toHaveTextContent("滚轮缩放");
    expect(container).not.toHaveTextContent("拖拽平移");

    await act(async () => {
      apiMocks.onBootstrap?.({
        ...bootstrap,
        map: {
          ...bootstrap.map,
          systems: bootstrap.map.systems.map((system) => ({
            ...system,
            hostile_count: 4,
          })),
          summary: {
            ...bootstrap.map.summary,
            hostile_count: 4,
          },
        },
      });
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    // Red-icon counts remain authoritative even when only one hostile identity
    // has been resolved in the alert payload.
    expect(situationStats).toHaveTextContent("当前有敌星系1");
    expect(situationStats).toHaveTextContent("当前敌对人数4");

    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);
    expect(apiMocks.connectAlerts).toHaveBeenCalledWith(
      expect.any(Function),
      "2026-07-02T12:00:00Z",
      undefined,
      expect.any(Function),
    );
    const nextAlert = {
      id: "alert-2",
      system_name: "0-UVHJ",
      system_id: 30003615,
      names: ["Pilot Two"],
      level: "medium",
      score: 50,
      created_at: "2026-07-02T12:02:00Z",
    } satisfies AlertItem;
    apiMocks.fetchBootstrap.mockResolvedValueOnce({
      ...bootstrap,
      alerts: [nextAlert, ...bootstrap.alerts],
    });

    await act(async () => {
      apiMocks.onAlert?.(nextAlert);
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);

    await act(async () => {
      apiMocks.onBootstrap?.({
        ...bootstrap,
        alerts: [],
        active_intel: [],
        reports: [],
        observations: [],
        map: {
          ...bootstrap.map,
          summary: {
            ...bootstrap.map.summary,
            alert_count: 0,
            hostile_count: 0,
            report_count: 0,
          },
        },
      });
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container.querySelector("#workbench-alert-panel")).not.toBeInTheDocument();
    expect(situationStats).toHaveTextContent("当前敌对人数0");
    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.unmount();
    });
    expect(apiMocks.close).toHaveBeenCalledTimes(1);
    container.remove();
  });

});
