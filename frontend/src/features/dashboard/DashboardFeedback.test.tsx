import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "./DashboardPage";
import type { BootstrapPayload } from "../workbench/types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fetchBootstrap = vi.hoisted(() => vi.fn());
vi.mock("../workbench/api", () => ({ fetchBootstrap }));

const emptyBootstrap: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1", generated_at: "2026-09-20T03:00:00Z",
  map: { schema_version: "map.v1", generated_at: "2026-09-20T03:00:00Z", systems: [], links: [], summary: {} },
  reports: [], alerts: [], clients: { count: 0, heartbeats: [], summary: { count: 0, online_count: 0, stale_count: 0 } },
  config: null, esi: { enabled: false, authenticated: false },
};

describe("dashboard data feedback", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let queryClient: QueryClient;

  beforeEach(() => {
    fetchBootstrap.mockReset();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    queryClient.clear();
    container.remove();
  });
  async function renderPage() {
    await act(async () => root.render(<QueryClientProvider client={queryClient}><DashboardPage /></QueryClientProvider>));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }

  it("does not present zero counts or a clear system while the first request is pending", async () => {
    fetchBootstrap.mockReturnValue(new Promise(() => {}));
    await renderPage();
    expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument();
    expect(container).toHaveTextContent("正在获取监控与敌情数据");
    expect(container).not.toHaveTextContent("当前监控范围内没有敌对");
    const values = container.querySelectorAll(".arco-statistic-value");
    expect(values).toHaveLength(4);
    values.forEach((value) => expect(value).toHaveTextContent("—"));
  });

  it("shows an explicit unavailable state after a failed initial request", async () => {
    fetchBootstrap.mockRejectedValue(new Error("offline"));
    await renderPage();
    expect(container).toHaveTextContent("数据暂不可用");
    expect(container).toHaveTextContent("尚未获取实时数据");
    expect(container).not.toHaveTextContent("当前监控范围内没有敌对");
  });

  it("distinguishes no monitoring coverage from a confirmed clear system", async () => {
    fetchBootstrap.mockResolvedValue(emptyBootstrap);
    await renderPage();
    expect(container).toHaveTextContent("暂无在线监控，暂不能判断星系是否安全");
    expect(container).toHaveTextContent("请先在客户端开启监控");
    expect(container).toHaveTextContent("最近成功获取");
    expect(container).not.toHaveTextContent("当前监控范围内没有敌对");
  });

  it("labels cached data after refresh failure without discarding the last snapshot", async () => {
    queryClient.setQueryData(["bootstrap"], emptyBootstrap);
    fetchBootstrap.mockRejectedValue(new Error("offline"));
    await renderPage();
    expect(container).toHaveTextContent("以下为上次成功获取的数据，不代表最新态势");
    expect(container).toHaveTextContent("上次数据");
    expect(container.querySelectorAll(".arco-statistic-value")[0]).toHaveTextContent("0");
  });

  it("supports manual recovery and links to existing pages", async () => {
    fetchBootstrap.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(emptyBootstrap);
    await renderPage();
    const refresh = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("刷新实时数据"))!;
    await act(async () => refresh.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    expect(fetchBootstrap).toHaveBeenCalledTimes(2);
    expect(container).toHaveTextContent("最近成功获取");
    expect(container).not.toHaveTextContent("实时态势数据加载失败");
    expect(container.querySelector('.dashboard-shortcuts a[href="/"]')).toHaveTextContent("打开星图");
    expect(container.querySelector('.dashboard-shortcuts a[href="/reports/history"]')).toHaveTextContent("来袭历史");
  });
});
