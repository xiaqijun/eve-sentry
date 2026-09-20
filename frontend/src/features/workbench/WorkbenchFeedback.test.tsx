import "@testing-library/jest-dom/vitest";
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchPage } from "./WorkbenchPage";
import { useWorkbenchStore } from "./store";
import { intelFeedback } from "./intelFeedback";
import type { BootstrapPayload } from "./types";
import type { TacticalGraphData } from "./tacticalGraph";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const api = vi.hoisted(() => ({ fetchBootstrap: vi.fn(), connectAlerts: vi.fn(), close: vi.fn() }));
vi.mock("./api", () => api);
vi.mock("./TacticalStarMap", () => ({ TacticalStarMap: ({ emptyContent, graphData, focusSystemId }: { emptyContent: ReactNode; graphData: TacticalGraphData; focusSystemId?: number }) =>
  <div data-testid="map" data-focus={focusSystemId}>{graphData.nodes.length === 0 ? emptyContent : "地图"}</div> }));
const empty: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1", generated_at: new Date().toISOString(),
  map: { schema_version: "map.v1", generated_at: new Date().toISOString(), systems: [], links: [], summary: {} },
  reports: [], alerts: [], config: null, esi: { enabled: false, authenticated: false },
  clients: { count: 0, heartbeats: [], summary: { count: 0, online_count: 0, stale_count: 0 } },
};
let query: QueryClient;
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let fail: (() => void) | undefined;
let update: ((data: BootstrapPayload) => void) | undefined;
beforeEach(() => {
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.fetchBootstrap.mockReset(); api.connectAlerts.mockReset(); api.close.mockReset();
  useWorkbenchStore.setState({ selectedSystemId: null });
  api.connectAlerts.mockImplementation((_alert, _since, onError, onBootstrap) => {
    fail = onError; update = onBootstrap; return { close: api.close };
  });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); query.clear(); });
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
async function open(path = "/") {
  await act(async () => root.render(<MemoryRouter initialEntries={[path]}><QueryClientProvider client={query}><WorkbenchPage /></QueryClientProvider></MemoryRouter>));
  await flush();
}
describe("star map feedback and location", () => {
  it("does not report zeros or safety before data arrives", async () => {
    api.fetchBootstrap.mockReturnValue(new Promise(() => {})); await open();
    expect(container).toHaveTextContent("正在获取监控与敌情数据…");
    expect(container.querySelectorAll(".star-map-status strong")[0]).toHaveTextContent("—");
    expect(container).not.toHaveTextContent("暂无实时敌对目标");
  });
  it("explains missing coverage and offers recovery paths", async () => {
    api.fetchBootstrap.mockResolvedValue(empty); await open();
    expect(container).toHaveTextContent("暂无在线监控，暂不能判断星系是否安全");
    expect(container.querySelector('a[href="/dashboard"]')).toHaveTextContent("查看监控覆盖");
  });
  it("labels initial failure and lets a refresh recover", async () => {
    api.fetchBootstrap.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(empty); await open();
    expect(container).toHaveTextContent("实时态势数据加载失败，请刷新后重试");
    const refresh = [...container.querySelectorAll("button")].find((button) => button.textContent === "刷新数据")!;
    await act(async () => refresh.click()); await flush();
    expect(container).toHaveTextContent("暂无在线监控，暂不能判断星系是否安全");
  });
  it("keeps stream failure visible until a new snapshot arrives without reconnecting on renders", async () => {
    api.fetchBootstrap.mockResolvedValue(empty); await open();
    expect(api.connectAlerts).toHaveBeenCalledTimes(1);
    await act(async () => fail?.());
    expect(container).toHaveTextContent("数据更新异常，当前显示上次数据，不代表最新态势");
    await act(async () => update?.(empty)); await flush();
    expect(container).toHaveTextContent("暂无在线监控，暂不能判断星系是否安全");
    expect(api.connectAlerts).toHaveBeenCalledTimes(1);
  });
  it("selects a system from the URL and clears the location on reset", async () => {
    api.fetchBootstrap.mockResolvedValue({ ...empty, map: { ...empty.map, systems: [{ name: "S-KSWL", system_id: 30003615, x: 1, y: 1 }] } });
    await open("/?system=S-KSWL");
    expect(container.querySelector('[data-testid="map"]')).toHaveAttribute("data-focus", "30003615");
    expect(useWorkbenchStore.getState().selectedSystemId).toBe(30003615);
    await act(async () => (container.querySelector('[aria-label="Fit 星图"]') as HTMLButtonElement).click());
    expect(container.querySelector('[data-testid="map"]')).not.toHaveAttribute("data-focus");
    expect(useWorkbenchStore.getState().selectedSystemId).toBeNull();
  });
  it("explains a missing target instead of claiming it was located", async () => {
    api.fetchBootstrap.mockResolvedValue(empty); await open("/?system=Missing");
    expect(container).toHaveTextContent("当前星图没有 Missing，未定位到该星系。");
  });
  it("only describes a clear monitoring area when coverage exists", () => {
    expect(intelFeedback(true, false, 1)).toBe("当前监控范围内没有敌对");
    expect(intelFeedback(true, true, 1)).toContain("不代表最新态势");
  });
});
