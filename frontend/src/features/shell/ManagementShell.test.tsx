import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ManagementShell } from "./ManagementShell";

const useAuthMock = vi.fn();
const changePasswordMock = vi.hoisted(() => vi.fn());

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));
vi.mock("../auth/api", () => ({
  changePassword: changePasswordMock,
}));

describe("ManagementShell", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    useAuthMock.mockReset();
    changePasswordMock.mockReset();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("renders the shared management navigation for an administrator", async () => {
    useAuthMock.mockReturnValue({
      authEnabled: true,
      logout: vi.fn(),
      refresh: vi.fn(),
      user: { display_name: "管理员", role: "admin", status: "active", username: "admin" },
    });

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/reports"]}>
          <Routes>
            <Route element={<ManagementShell />} path="/">
              <Route element={<div>报表内容</div>} path="reports" />
            </Route>
          </Routes>
        </MemoryRouter>,
      );
    });

    expect(container).toHaveTextContent("EVE Sentry");
    expect(container).toHaveTextContent("仪表盘");
    expect(container).toHaveTextContent("星图态势");
    expect(container).toHaveTextContent("来袭报表");
    expect(container).toHaveTextContent("设备密钥");
    expect(container).not.toHaveTextContent("账号安全");
    expect(container).toHaveTextContent("用户管理");
    expect(container).toHaveTextContent("身份记录");
    expect(container).toHaveTextContent("白名单管理");
    expect(container).toHaveTextContent("审计日志");
    expect(container).toHaveTextContent("报表内容");
    expect(container.querySelector('a[href="/reports"]')).toHaveClass("active");
    expect(container.querySelector('a[href="/dashboard"]')).toBeInTheDocument();
    expect(container.querySelector('a[href="/admin/whitelist"]')).toBeInTheDocument();
    expect(container.querySelector('a[href="/account/security"]')).not.toBeInTheDocument();

    const accountButton = container.querySelector('[aria-label="账号设置"]') as HTMLButtonElement;
    await act(async () => accountButton.click());
    expect(container.querySelector('[role="dialog"][aria-label="账号设置"]')).toBeInTheDocument();
    expect(container).toHaveTextContent("修改登录密码");
    expect(container).toHaveTextContent("退出登录");
  });

  it("hides account administration in public mode", async () => {
    useAuthMock.mockReturnValue({ authEnabled: false, logout: vi.fn(), refresh: vi.fn(), user: null });

    await act(async () => {
      root.render(
        <MemoryRouter>
          <Routes>
            <Route element={<ManagementShell />} path="/">
              <Route index element={<div>工作台内容</div>} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );
    });

    expect(container).toHaveTextContent("公开模式");
    expect(container).toHaveTextContent("工作台内容");
    expect(container).not.toHaveTextContent("设备密钥");
    expect(container).not.toHaveTextContent("账号安全");
    expect(container).not.toHaveTextContent("用户管理");
    expect(container).not.toHaveTextContent("身份记录");
    expect(container).not.toHaveTextContent("白名单管理");
    expect(container).not.toHaveTextContent("审计日志");
    expect(container.querySelector('[aria-label="账号设置"]')).not.toBeInTheDocument();
  });

  it("keeps password settings hidden for EVE member accounts", async () => {
    useAuthMock.mockReturnValue({
      authEnabled: true,
      logout: vi.fn(),
      refresh: vi.fn(),
      user: { display_name: "值班员", role: "member", status: "active", username: "watcher" },
    });

    await act(async () => {
      root.render(
        <MemoryRouter>
          <Routes>
            <Route element={<ManagementShell />} path="/">
              <Route index element={<div>工作台内容</div>} />
            </Route>
          </Routes>
        </MemoryRouter>,
      );
    });

    expect(container).toHaveTextContent("设备密钥");
    expect(container).not.toHaveTextContent("账号安全");
    expect(container).not.toHaveTextContent("用户管理");

    const accountButton = container.querySelector('[aria-label="账号设置"]') as HTMLButtonElement;
    await act(async () => accountButton.click());
    expect(container).toHaveTextContent("EVE SSO");
    expect(container).toHaveTextContent("退出登录");
    expect(container).not.toHaveTextContent("修改登录密码");
  });
});
