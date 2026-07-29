import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HostileReportPage } from "./HostileReportPage";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const apiMocks = vi.hoisted(() => ({
  fetchHostileAlertHistory: vi.fn(),
}));

vi.mock("./api", () => ({
  fetchHostileAlertHistory: apiMocks.fetchHostileAlertHistory,
}));
vi.mock("../../components/EveChart", () => ({
  EveChart: ({ className }: { className?: string }) => (
    <div className={className} data-testid="eve-chart" />
  ),
}));

describe("HostileReportPage", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.clearAllMocks();
  });

  it("renders hostile incursion totals, rankings, and recent records", async () => {
    const now = Date.now();
    apiMocks.fetchHostileAlertHistory.mockResolvedValue({
      alerts: [
        {
          id: "alert-1",
          system_name: "S-KSWL",
          names: ["Alice", "Bob"],
          character_ids: [101, 102],
          verified_characters: [
            { character_id: 101, name: "Alice" },
            { character_id: 102, name: "Bob" },
          ],
          level: "high",
          classification: "red",
          created_at: new Date(now - 10 * 60 * 1000).toISOString(),
          acknowledged: false,
        },
        {
          id: "alert-2",
          system_name: "S-KSWL",
          names: ["Alice"],
          character_ids: [101],
          verified_characters: [{ character_id: 101, name: "Alice" }],
          level: "low",
          classification: "red",
          created_at: new Date(now - 60 * 60 * 1000).toISOString(),
          acknowledged: true,
        },
      ],
      count: 2,
      generatedAt: new Date(now).toISOString(),
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
          <MemoryRouter>
            <HostileReportPage />
          </MemoryRouter>
        </QueryClientProvider>,
      );
    });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container).toHaveTextContent("敌对来袭报表");
    expect(container).toHaveTextContent("仅统计 ESI 已验证敌对角色");
    expect(container).toHaveTextContent("有效来袭");
    expect(container).toHaveTextContent("独立敌对");
    expect(container).toHaveTextContent("高危事件");
    expect(container).toHaveTextContent("数据有效性");
    expect(container).toHaveTextContent("有效数据率");
    expect(container).toHaveTextContent("排除噪声");
    expect(container).toHaveTextContent("有效来袭趋势");
    expect(container).toHaveTextContent("风险分布");
    expect(container).toHaveTextContent("星系来袭排行");
    expect(container).toHaveTextContent("高频敌对目标");
    expect(container).toHaveTextContent("最近有效来袭");
    expect(container).toHaveTextContent("S-KSWL");
    expect(container).toHaveTextContent("Alice");
    expect(container).toHaveTextContent("Bob");
    expect(container.querySelectorAll('[data-testid="eve-chart"]')).toHaveLength(2);
    expect(apiMocks.fetchHostileAlertHistory).toHaveBeenCalledWith("7d");

    await act(async () => root.unmount());
  });
});
