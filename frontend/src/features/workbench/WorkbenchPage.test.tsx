import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, forwardRef } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, describe, expect, test, vi } from "vitest";

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
    connectAlerts: vi.fn((onAlert: (alert: AlertItem) => void, _since?: string) => {
      apiMocks.onAlert = onAlert;
      return { close };
    }),
    fetchBootstrap: vi.fn(async () => bootstrap),
    fetchEsiLoginStatus: vi.fn(async () => ({
      status: "pending",
      authorization_url: "https://login.test/authorize",
    })),
    startEsiLogin: vi.fn(async () => ({
      status: "pending",
      authorization_url: "https://login.test/authorize",
    })),
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
  fetchEsiLoginStatus: apiMocks.fetchEsiLoginStatus,
  startEsiLogin: apiMocks.startEsiLogin,
}));

describe("WorkbenchPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.onAlert = undefined;
    apiMocks.fetchBootstrap.mockImplementation(async () => bootstrap);
    apiMocks.fetchEsiLoginStatus.mockImplementation(async () => ({
      status: "pending",
      authorization_url: "https://login.test/authorize",
    }));
    apiMocks.startEsiLogin.mockImplementation(async () => ({
      status: "pending",
      authorization_url: "https://login.test/authorize",
    }));
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
    expect(container.querySelector('[data-testid="observation-table"]')).toBeInTheDocument();
    expect(container).toHaveTextContent("Pilot One");
    expect(container).toHaveTextContent("预警频道");
    expect(container).toHaveTextContent("预警情报工作台");
    expect(container).toHaveTextContent("实时态势侧栏");
    expect(container).toHaveTextContent("敌对飞行员观察列表");
    expect(container).toHaveTextContent("ESI 状态");
    expect(container).toHaveTextContent("未授权");
    expect(container).toHaveTextContent("Client ID");
    expect(container).toHaveTextContent("未配置");
    expect(container).toHaveTextContent("Token");
    expect(container).toHaveTextContent("未保存");
    expect(container.querySelector(".esi-login-button")).toHaveTextContent("登录 ESI");
    expect(container.querySelector(".esi-login-button")).toBeDisabled();

    expect(container.querySelector(".nav-panel")).not.toBeInTheDocument();
    expect(container.querySelector(".quick-icons")).not.toBeInTheDocument();
    expect(container.querySelector(".sector-preview")).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="工作台态势侧栏"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换区域"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换视图模式"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="切换视图"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="列表视图"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="设置"]')).not.toBeInTheDocument();
    expect(container.querySelector('[aria-label="情报过滤"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="Fit 星图"]')).toBeInTheDocument();
    expect(container).not.toHaveTextContent("滚轮缩放");
    expect(container).not.toHaveTextContent("拖拽平移");

    expect(apiMocks.connectAlerts).toHaveBeenCalledTimes(1);
    expect(apiMocks.connectAlerts).toHaveBeenCalledWith(
      expect.any(Function),
      "2026-07-02T12:00:00Z",
    );
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

  test("opens the ESI authorization URL from the login button", async () => {
    apiMocks.fetchBootstrap.mockResolvedValueOnce({
      ...bootstrap,
      esi: {
        ...bootstrap.esi,
        config: {
          ...bootstrap.esi.config,
          client_id_configured: true,
        },
      },
    });
    const fakeWindow = {
      close: vi.fn(),
      location: { href: "" },
      opener: undefined,
    } as unknown as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(fakeWindow);
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

    const button = container.querySelector(".esi-login-button") as HTMLButtonElement;
    expect(button).not.toBeDisabled();

    await act(async () => {
      button.click();
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(apiMocks.startEsiLogin).toHaveBeenCalledTimes(1);
    expect(openSpy).toHaveBeenCalledWith("", "_blank");
    expect(fakeWindow.location.href).toBe("https://login.test/authorize");
    expect(button).toHaveTextContent("等待授权");
    expect(container).toHaveTextContent("等待 EVE 授权回调");
    expect(apiMocks.fetchEsiLoginStatus).toHaveBeenCalledTimes(1);

    await act(async () => {
      root.unmount();
    });
    openSpy.mockRestore();
    container.remove();
  });

  test("does not show the bootstrap refresh time as a latest event time", async () => {
    apiMocks.fetchBootstrap.mockResolvedValueOnce({
      ...bootstrap,
      active_intel: [],
      alerts: [],
      observations: [],
      reports: [],
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

    const latestEvent = container.querySelector(".latest-event");
    expect(latestEvent).toHaveClass("is-empty");
    expect(latestEvent).toHaveTextContent("--:--");
    expect(latestEvent).toHaveTextContent("暂无实时威胁事件");
    expect(latestEvent).not.toHaveTextContent("12:00");

    await act(async () => {
      root.unmount();
    });
    container.remove();
  });
});
