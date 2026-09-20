import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchPage } from "./WorkbenchPage";
import type { BootstrapPayload } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const api = vi.hoisted(() => ({ fetchBootstrap: vi.fn(), connectAlerts: vi.fn() }));
vi.mock("./api", () => api);
vi.mock("./TacticalStarMap", () => ({ TacticalStarMap: () => <div data-testid="map">星图</div> }));
const snapshot: BootstrapPayload = {
  schema_version: "intel_bootstrap.v1", generated_at: new Date().toISOString(),
  map: { schema_version: "map.v1", generated_at: new Date().toISOString(), systems: [{ name: "S-KSWL", system_id: 30003615, x: 1, y: 1 }], links: [], summary: {} },
  reports: [], alerts: [], config: null, esi: { enabled: false, authenticated: false },
  clients: { count: 0, heartbeats: [], summary: { count: 0, online_count: 0, stale_count: 0 } },
};
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
let query: QueryClient;
let streamError: () => void;
beforeEach(() => {
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.fetchBootstrap.mockResolvedValue(snapshot);
  api.connectAlerts.mockImplementation((_alert, _since, onError) => { streamError = onError; return { close: vi.fn() }; });
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); query.clear(); });
async function openMap() {
  await act(async () => root.render(<MemoryRouter initialEntries={["/?system=S-KSWL"]}><QueryClientProvider client={query}><WorkbenchPage /></QueryClientProvider></MemoryRouter>));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}
describe("nonblocking coverage status", () => {
  it("keeps zero coverage inside the statistics instead of over the map", async () => {
    await openMap();
    expect(container.querySelector(".star-map-feedback-banner")).not.toBeInTheDocument();
    expect(container.querySelector(".star-map-status")).toHaveTextContent("未覆盖");
    expect(container.querySelector(".star-map-coverage-hint")).toHaveAttribute("tabindex", "0");
    expect(container).not.toHaveTextContent("暂不能判断星系是否安全");
    expect(container.querySelector(".star-map-selection")).toHaveTextContent("S-KSWL");
  });
  it("still warns about an actual stream failure", async () => {
    await openMap();
    await act(async () => streamError());
    expect(container.querySelector('[role="alert"]')).toHaveTextContent("不代表最新态势");
    expect(container.querySelector(".star-map-coverage-hint")).not.toBeInTheDocument();
  });
});
