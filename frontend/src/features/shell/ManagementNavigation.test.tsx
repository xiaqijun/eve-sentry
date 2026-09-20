import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ManagementShell } from "./ManagementShell";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const auth = vi.hoisted(() => vi.fn());
vi.mock("../auth/AuthContext", () => ({ useAuth: auth }));
vi.mock("../auth/api", () => ({ changePassword: vi.fn() }));

describe("responsive management navigation", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    auth.mockReturnValue({ authEnabled: true, user: { username: "admin", display_name: "管理员", role: "admin" }, logout: vi.fn() });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });
  async function renderShell() {
    await act(async () => root.render(
      <MemoryRouter initialEntries={["/dashboard"]}>
        <Routes><Route element={<ManagementShell />}>
          <Route path="dashboard" element={<p>工作台正文</p>} />
          <Route path="admin/clients" element={<p>设备页面正文</p>} />
        </Route></Routes>
      </MemoryRouter>,
    ));
    return container.querySelector('[aria-label="打开导航菜单"]') as HTMLButtonElement;
  }

  it("offers a skip link and labels authentication mode without claiming service health", async () => {
    await renderShell();
    expect(container).toHaveTextContent("登录模式");
    expect(container).not.toHaveTextContent("服务在线");
    expect(container.querySelector('a[href="#management-content"]')).toBeInTheDocument();
    expect(container.querySelector('#management-content[tabindex="-1"]')).toHaveTextContent("工作台正文");
  });

  it("opens labeled navigation and closes after choosing a page", async () => {
    const trigger = await renderShell();
    await act(async () => trigger.click());
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const drawer = document.querySelector('[role="dialog"][aria-label="页面导航"]')!;
    expect(drawer).toHaveTextContent("客户端管理");
    const link = drawer.querySelector('a[href="/admin/clients"]') as HTMLAnchorElement;
    await act(async () => link.click());
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(container).toHaveTextContent("设备页面正文");
  });

  it("closes the drawer with Escape", async () => {
    const trigger = await renderShell();
    await act(async () => trigger.click());
    const drawer = document.querySelector('[role="dialog"][aria-label="页面导航"]')!;
    await act(async () => drawer.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true })));
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps administrative links out of the public drawer", async () => {
    auth.mockReturnValue({ authEnabled: false, user: null, logout: vi.fn() });
    const trigger = await renderShell();
    await act(async () => trigger.click());
    const drawer = document.querySelector('[role="dialog"][aria-label="页面导航"]')!;
    expect(drawer).not.toHaveTextContent("用户管理");
    expect(drawer).not.toHaveTextContent("设备密钥");
    expect(drawer).toHaveTextContent("工作台");
  });
});
