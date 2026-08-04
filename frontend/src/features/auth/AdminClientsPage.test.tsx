import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminClientsPage, filterAdminClients } from "./AdminClientsPage";
import type { AdminClientHeartbeatRecord, AdminClientsSnapshot } from "./types";

const listAdminClientsMock = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({
  listAdminClients: listAdminClientsMock,
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

const owner = {
  display_name: "舰队管理员",
  must_change_password: false,
  role: "admin" as const,
  status: "active" as const,
  user_id: "user-1",
  username: "admin",
};

const clients: AdminClientHeartbeatRecord[] = [
  {
    client_id: "detector-client:alpha",
    client_type: "detector_client",
    label: "阿尔法监控端",
    online: true,
    owner,
    key: {
      created_at: "2026-08-01T08:00:00Z",
      identity_verified: true,
      key_id: "key-1",
      key_prefix: "eve_alpha",
      key_type: "desktop",
      name: "阿尔法设备",
      status: "active",
      user_id: "user-1",
    },
    api_key_id: "key-1",
    remote_ip: "10.0.0.8",
    seen_at: "2026-08-04T08:00:00Z",
    status: "running",
    details: {
      client_version: "1.0.20",
      host: "SCOUT-PC",
      last_action: "ocr_snapshot:3",
      targets: [{ character_name: "Alice", system_name: "Tama", monitoring: true }],
    },
  },
  {
    client_id: "alert-client:bravo",
    client_type: "alert_client",
    label: "布拉沃预警端",
    online: false,
    seen_at: "2026-08-03T08:00:00Z",
    status: "idle",
    user_id: "user-2",
    details: { client_version: "1.0.19", host: "ALERT-PC", last_error: "heartbeat timeout" },
  },
];

const snapshot: AdminClientsSnapshot = {
  clients: { count: clients.length, heartbeats: clients },
  keys: [{
    key: clients[0].key!,
    owner,
    client_count: 2,
    linked_clients: clients,
    online_count: 1,
    last_client: clients[0],
    last_ip: "10.0.0.8",
  }],
};

describe("AdminClientsPage", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot> | null;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    listAdminClientsMock.mockReset();
  });

  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    vi.useRealTimers();
    container.remove();
  });

  async function renderPage() {
    await act(async () => {
      root?.render(<AdminClientsPage />);
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("renders client ownership, runtime and monitoring targets", async () => {
    listAdminClientsMock.mockResolvedValue(snapshot);
    await renderPage();

    expect(container).toHaveTextContent("阿尔法监控端");
    expect(container).toHaveTextContent("舰队管理员");
    expect(container).toHaveTextContent("阿尔法设备");
    expect(container).toHaveTextContent("1.0.20");
    expect(container).toHaveTextContent("Alice · Tama");
    expect(container).toHaveTextContent("ocr_snapshot:3");
    expect(container.querySelector('[aria-label="搜索客户端"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="客户端类型"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="在线状态"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="所属用户"]')).toBeInTheDocument();
  });

  it("filters by search, type, online state and owner", () => {
    expect(filterAdminClients(clients, {
      search: "Tama",
      clientType: "detector_client",
      online: "online",
      userId: "user-1",
    })).toEqual([clients[0]]);

    expect(filterAdminClients(clients, {
      search: "",
      clientType: "alert_client",
      online: "offline",
      userId: "user-2",
    })).toEqual([clients[1]]);

    expect(filterAdminClients(clients, {
      search: "不存在",
      clientType: "all",
      online: "all",
      userId: "all",
    })).toEqual([]);
  });

  it("shows an explicit empty state when no heartbeat has been received", async () => {
    listAdminClientsMock.mockResolvedValue({
      clients: { count: 0, heartbeats: [] },
      keys: [],
    });
    await renderPage();

    expect(container).toHaveTextContent("没有符合筛选条件的客户端");
    expect(container).toHaveTextContent("0 / 0 个实例");
  });

  it("renders key usage after switching tabs", async () => {
    listAdminClientsMock.mockResolvedValue(snapshot);
    await renderPage();
    const keyTab = Array.from(container.querySelectorAll<HTMLElement>('[role="tab"]'))
      .find((tab) => tab.textContent?.includes("密钥使用"));

    await act(async () => keyTab?.click());

    expect(container).toHaveTextContent("eve_alpha");
    expect(container).toHaveTextContent("2");
    expect(container).toHaveTextContent("1 在线");
    expect(container).toHaveTextContent("10.0.0.8");
    expect(container.querySelector('[aria-label="搜索密钥使用"]')).toBeInTheDocument();
  });

  it("refreshes client status in the background and clears the timer on unmount", async () => {
    vi.useFakeTimers();
    const refreshedSnapshot: AdminClientsSnapshot = {
      ...snapshot,
      clients: {
        ...snapshot.clients,
        heartbeats: [{
          ...clients[0],
          online: false,
          details: { ...clients[0].details, last_action: "heartbeat_refreshed" },
        }, clients[1]],
      },
    };
    let resolveRefresh: (value: AdminClientsSnapshot) => void = () => undefined;
    listAdminClientsMock
      .mockResolvedValueOnce(snapshot)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRefresh = resolve; }));
    await renderPage();

    expect(listAdminClientsMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="查看 阿尔法监控端"]')?.click();
    });
    const drawer = document.body.querySelector(".admin-client-drawer");
    expect(drawer).toHaveTextContent("ocr_snapshot:3");

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
    });

    expect(listAdminClientsMock).toHaveBeenCalledTimes(2);
    const refreshButton = Array.from(container.querySelectorAll("button"))
      .find((button) => button.textContent?.includes("刷新客户端"));
    expect(refreshButton).not.toHaveClass("arco-btn-loading");
    expect(drawer).toHaveTextContent("ocr_snapshot:3");

    await act(async () => {
      resolveRefresh(refreshedSnapshot);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container).toHaveTextContent("阿尔法监控端");
    expect(drawer).toHaveTextContent("heartbeat_refreshed");

    await act(async () => root?.unmount());
    root = null;
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(listAdminClientsMock).toHaveBeenCalledTimes(2);
  });

  it("paginates clients and returns to the first page when filters change", async () => {
    const pagedClients = Array.from({ length: 25 }, (_, index) => ({
      ...clients[0],
      client_id: `detector-client:${index + 1}`,
      label: `巡逻客户端 ${String(index + 1).padStart(2, "0")}`,
    }));
    listAdminClientsMock.mockResolvedValue({
      clients: { count: pagedClients.length, heartbeats: pagedClients },
      keys: [],
    });
    await renderPage();

    const secondPage = Array.from(container.querySelectorAll<HTMLElement>(".arco-pagination-item"))
      .find((item) => item.textContent?.trim() === "2");
    expect(secondPage).toBeDefined();
    expect(container).toHaveTextContent("巡逻客户端 01");
    expect(container).not.toHaveTextContent("巡逻客户端 21");

    await act(async () => secondPage?.click());
    expect(container).toHaveTextContent("巡逻客户端 21");
    expect(container).not.toHaveTextContent("巡逻客户端 01");

    const searchInput = container.querySelector<HTMLInputElement>('[aria-label="搜索客户端"]');
    await act(async () => {
      const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      valueSetter?.call(searchInput, "巡逻客户端");
      searchInput?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(container.querySelector(".arco-pagination-item-active")).toHaveTextContent("1");
    expect(container).toHaveTextContent("巡逻客户端 01");
    expect(container).not.toHaveTextContent("巡逻客户端 21");
  });
});
