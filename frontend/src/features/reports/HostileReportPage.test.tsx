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
            {
              character_id: 101,
              name: "Alice",
              zkill: { danger_ratio: 82, ships_destroyed: 320, ships_lost: 18 },
            },
            { character_id: 102, name: "Bob" },
          ],
          level: "high",
          classification: "red",
          created_at: new Date(now - 10 * 60 * 1000).toISOString(),
        },
        {
          id: "alert-2",
          system_name: "S-KSWL",
          names: ["Alice"],
          character_ids: [101],
          verified_characters: [{
            character_id: 101,
            name: "Alice",
            zkill: { danger_ratio: 82, ships_destroyed: 320, ships_lost: 18 },
          }],
          level: "low",
          classification: "red",
          created_at: new Date(now - 60 * 60 * 1000).toISOString(),
        },
      ],
      waves: [
        {
          id: "wave-1",
          system_name: "S-KSWL",
          started_at: new Date(now - 70 * 60 * 1000).toISOString(),
          last_seen_at: new Date(now - 10 * 60 * 1000).toISOString(),
          cleared_at: new Date(now - 5 * 60 * 1000).toISOString(),
          active: false,
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

    expect(container).toHaveTextContent("来袭分析");
    expect(container).toHaveTextContent("历史研判");
    expect(container).toHaveTextContent("有效来袭事件");
    expect(container).toHaveTextContent("来袭波次");
    expect(container).toHaveTextContent("敌对出现至清空为一波");
    expect(container).toHaveTextContent("独立敌对人员");
    expect(container).toHaveTextContent("来袭趋势");
    expect(container).toHaveTextContent("热点星系");
    expect(container).toHaveTextContent("人员研判");
    expect(
      [...container.querySelectorAll(".arco-tag")].some(
        (item) => item.textContent?.trim() === "82",
      ),
    ).toBe(true);
    expect(container).not.toHaveTextContent("威胁度 82");
    expect(container.querySelector(".combat-summary")).toHaveTextContent("320 / 18");
    expect(container).toHaveTextContent("S-KSWL");
    expect(container).toHaveTextContent("Alice");
    expect(container).toHaveTextContent("Bob");
    expect(container.querySelectorAll('[data-testid="eve-chart"]')).toHaveLength(1);
    expect(apiMocks.fetchHostileAlertHistory).toHaveBeenCalledWith("24h");

    await act(async () => root.unmount());
  });
});
