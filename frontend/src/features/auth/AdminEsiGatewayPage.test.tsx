import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminEsiGatewayPage } from "./AdminEsiGatewayPage";
import type { EsiGatewaySnapshot } from "./types";

const fetchEsiGatewayMock = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({ fetchEsiGateway: fetchEsiGatewayMock }));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const snapshot: EsiGatewaySnapshot = {
  gateway: {
    configured: true,
    reachable: true,
    url: "http://10.233.53.17:8787",
    checked_at: "2026-08-28T08:00:00Z",
    health: {
      service: "eve-sentry-esi-gateway",
      version: "1.0",
      uptime_seconds: 3661,
      requests: 12,
      errors: 1,
      cache_hits: 8,
      cache_entries: 4,
      cache_hit_rate: 0.6667,
      rate_limit_per_second: 2,
      latency_ms: { last: 31, average: 94 },
    },
  },
  resolver_cache: {
    personnel: { lookups: 16, hits: 12, misses: 4, hit_rate: 0.75 },
    totals: { lookups: 20, hits: 15, misses: 5, hit_rate: 0.75 },
    entries: { total: 30, active: 28, stale: 2 },
    namespaces: {
      name: {
        lookups: 16,
        hits: 12,
        misses: 4,
        hit_rate: 0.75,
        active_entries: 20,
        stale_entries: 1,
      },
    },
  },
  client_metrics: {
    durations_ms: { get_system: { count: 3, last: 31, p50: 40, p95: 160 } },
  },
};

describe("AdminEsiGatewayPage", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot> | null;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    fetchEsiGatewayMock.mockReset();
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    vi.useRealTimers();
    container.remove();
  });

  async function renderPage() {
    await act(async () => {
      root?.render(<AdminEsiGatewayPage />);
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("renders gateway status, metrics and endpoint latency", async () => {
    fetchEsiGatewayMock.mockResolvedValue(snapshot);
    await renderPage();

    expect(container).toHaveTextContent("网关在线");
    expect(container).toHaveTextContent("12");
    expect(container).toHaveTextContent("67%");
    expect(container).toHaveTextContent("114 名单命中率");
    expect(container).toHaveTextContent("人员名称");
    expect(container).toHaveTextContent("75%");
    expect(container).toHaveTextContent("eve-sentry-esi-gateway");
    expect(container).toHaveTextContent("get_system");
    expect(container).toHaveTextContent("P95 (ms)");
  });

  it("shows a recoverable warning when gateway is unavailable", async () => {
    fetchEsiGatewayMock.mockResolvedValue({
      ...snapshot,
      gateway: { ...snapshot.gateway, reachable: false, error: "connection refused" },
    });
    await renderPage();

    expect(container).toHaveTextContent("网关不可达");
    expect(container).toHaveTextContent("connection refused");
  });

  it("keeps the last health snapshot when a later probe cannot reach the gateway", async () => {
    vi.useFakeTimers();
    fetchEsiGatewayMock
      .mockResolvedValueOnce(snapshot)
      .mockResolvedValueOnce({
        gateway: { configured: true, reachable: false, error: "timeout" },
        client_metrics: {},
      });
    await renderPage();

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container).toHaveTextContent("网关不可达");
    expect(container).toHaveTextContent("12");
    expect(container).toHaveTextContent("timeout");
  });
});
