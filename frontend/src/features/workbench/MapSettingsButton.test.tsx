import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MapSettingsButton } from "./MapSettingsButton";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const api = vi.hoisted(() => ({ fetchMe: vi.fn(), apiRequest: vi.fn() }));
vi.mock("../auth/api", () => api);
const settings = { enabled: true, version: "v1", region_ids: [10], system_ids: [3], excluded_system_ids: [2],
  regions: [{ id: 10, name: "Tenal" }], systems: [{ id: 1, name: "A", region_id: 10 }, { id: 2, name: "B", region_id: 10 }, { id: 3, name: "C", region_id: 20 }] };
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let query: QueryClient;
beforeEach(() => {
  vi.clearAllMocks(); container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.fetchMe.mockResolvedValue({ role: "admin" }); api.apiRequest.mockResolvedValue({ settings });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); query.clear(); });
async function render() {
  await act(async () => root.render(<QueryClientProvider client={query}><MapSettingsButton /></QueryClientProvider>));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
async function click(text: string) {
  const button = [...document.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  expect(button).toBeTruthy(); await act(async () => button!.click());
}
describe("map region settings", () => {
  it("is visible only to administrators", async () => {
    api.fetchMe.mockResolvedValue({ role: "member" }); await render();
    expect(container).not.toHaveTextContent("监控范围"); expect(api.apiRequest).not.toHaveBeenCalled();
  });
  it("previews union minus exclusions and submits IDs with revision", async () => {
    await render(); await click("监控范围");
    expect(document.body).toHaveTextContent("当前星图覆盖 2 个星系");
    await click("保存并应用");
    expect(api.apiRequest).toHaveBeenLastCalledWith("/api/v1/admin/map-settings", { method: "PUT", body: JSON.stringify({ enabled: true, version: "v1", region_ids: [10], system_ids: [3], excluded_system_ids: [2] }) });
  });
  it("does not close the dialog when save fails", async () => {
    await render(); await click("监控范围");
    api.apiRequest.mockRejectedValueOnce(new Error("配置已被修改")); await click("保存并应用");
    expect(document.body).toHaveTextContent("配置已被修改");
    expect(document.body).toHaveTextContent("重新加载配置");
  });
  it("disables saving an empty effective scope", async () => {
    api.apiRequest.mockResolvedValue({ settings: { ...settings, region_ids: [], system_ids: [] } });
    await render(); await click("监控范围");
    expect([...document.querySelectorAll("button")].find((item) => item.textContent?.includes("保存并应用"))).toBeDisabled();
  });
});
