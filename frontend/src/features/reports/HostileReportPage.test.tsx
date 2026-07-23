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
    expect(container).toHaveTextContent("所有统计仅包含 ESI 已确认存在的角色");
    expect(container).toHaveTextContent("来袭批次");
    expect(container).toHaveTextContent("目标人次");
    expect(container).toHaveTextContent("独立敌对");
    expect(container).toHaveTextContent("S-KSWL");
    expect(container).toHaveTextContent("Alice");
    expect(container).toHaveTextContent("Bob");
    expect(container.querySelector('[aria-label="敌对来袭时间趋势"]')).toBeInTheDocument();
    expect(apiMocks.fetchHostileAlertHistory).toHaveBeenCalledWith("7d");

    await act(async () => root.unmount());
  });
});
