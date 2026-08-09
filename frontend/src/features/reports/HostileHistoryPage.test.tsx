import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HostileHistoryPage } from "./HostileHistoryPage";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const fetchHostileAlertHistory = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({ fetchHostileAlertHistory }));

describe("HostileHistoryPage", () => {
  afterEach(() => {
    document.body.innerHTML = "";
    vi.clearAllMocks();
  });

  it("queries and filters historical waves and personnel alerts", async () => {
    const now = Date.now();
    fetchHostileAlertHistory.mockResolvedValue({
      alerts: [
        {
          id: "alert-1",
          system_name: "Jita",
          names: ["Alice"],
          verified_characters: [{ character_id: 1, name: "Alice" }],
          level: "high",
          classification: "red",
          created_at: new Date(now - 4 * 60 * 1000).toISOString(),
        },
        {
          id: "alert-2",
          system_name: "Amarr",
          names: ["Bob"],
          verified_characters: [{ character_id: 2, name: "Bob" }],
          level: "low",
          classification: "red",
          created_at: new Date(now - 3 * 60 * 1000).toISOString(),
        },
      ],
      waves: [{
        id: "wave-1",
        system_name: "Jita",
        started_at: new Date(now - 5 * 60 * 1000).toISOString(),
        last_seen_at: new Date(now - 2 * 60 * 1000).toISOString(),
        cleared_at: new Date(now - 1 * 60 * 1000).toISOString(),
        active: false,
      }],
      generatedAt: new Date(now).toISOString(),
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <HostileHistoryPage />
          </MemoryRouter>
        </QueryClientProvider>,
      );
    });
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    });

    expect(container).toHaveTextContent("来袭历史");
    expect(container).toHaveTextContent("来袭波次（1）");
    expect(container).toHaveTextContent("人员告警（2）");
    expect(container).toHaveTextContent("Jita");
    expect(container).toHaveTextContent("Amarr");
    expect(fetchHostileAlertHistory).toHaveBeenCalledWith("24h");

    const search = container.querySelector<HTMLInputElement>('input[placeholder="搜索星系或人员"]');
    expect(search).toBeInTheDocument();
    await act(async () => {
      const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      valueSetter?.call(search, "Amarr");
      search?.dispatchEvent(new Event("input", { bubbles: true }));
      await Promise.resolve();
    });
    expect(container).toHaveTextContent("人员告警（1）");

    await act(async () => root.unmount());
  });
});
