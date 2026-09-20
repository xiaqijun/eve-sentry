import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "./DashboardPage";
import type { BootstrapPayload } from "../workbench/types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const api = vi.hoisted(() => ({ fetchBootstrap: vi.fn() }));
vi.mock("../workbench/api", () => api);
const snapshot: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1", generated_at: new Date().toISOString(),
  map: { schema_version: "map.v1", generated_at: new Date().toISOString(), systems: [], links: [], summary: {} },
  reports: [], alerts: [{ id: "a", system_name: "S-KSWL", classification: "red", verified_characters: [{ name: "Alice", character_id: 101 }] }],
  clients: { count: 1, summary: { count: 1, online_count: 1, stale_count: 0 }, heartbeats: [{
    client_id: "client-1", client_type: "detector_client", online: true, label: "演示设备",
    details: { monitoring: true, targets: [{ character_name: "Scout", system_name: "S-KSWL", monitoring: true }] },
  }] }, config: null, esi: { enabled: false, authenticated: false },
};
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let query: QueryClient;
beforeEach(() => {
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.fetchBootstrap.mockResolvedValue(snapshot);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); query.clear(); });
async function flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
async function renderPage() {
  await act(async () => root.render(<QueryClientProvider client={query}><DashboardPage /></QueryClientProvider>));
  await flush();
}
describe("dashboard direct actions", () => {
  it("opens node details from the snapshot and handles a disappearing node", async () => {
    await renderPage();
    await act(async () => (container.querySelector('[aria-label="查看监控节点 1详情"]') as HTMLButtonElement).click());
    expect(document.body).toHaveTextContent("Scout");
    expect(document.body).toHaveTextContent("监控节点详情");
    await act(async () => { query.setQueryData(["bootstrap"], { ...snapshot, clients: { ...snapshot.clients, heartbeats: [] } }); });
    await flush();
    expect(document.body).toHaveTextContent("该节点已不在当前列表中，请关闭后刷新查看");
  });
  it("keeps duplicate alert detail collapsed with a native summary control", async () => {
    await renderPage();
    expect(container.querySelector("details")).not.toHaveAttribute("open");
    expect(container.querySelector("details summary")).toHaveTextContent("展开当前告警明细（1 条）");
    expect(container.querySelector("h1")).toHaveTextContent("工作台");
    expect(container).not.toHaveTextContent("最高威胁度");
  });
});
