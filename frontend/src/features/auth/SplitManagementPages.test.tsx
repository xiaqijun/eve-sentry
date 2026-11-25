import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountKeysPage } from "./AccountKeysPage";
import { AdminUsersPage } from "./AdminUsersPage";

const apiMocks = vi.hoisted(() => ({
  createUser: vi.fn(),
  deleteKey: vi.fn(),
  deleteUser: vi.fn(),
  enableKey: vi.fn(),
  listAdminUsers: vi.fn(),
  listMyKeys: vi.fn(),
  resetUserPassword: vi.fn(),
  revokeKey: vi.fn(),
  setUserActive: vi.fn(),
}));

vi.mock("./api", () => apiMocks);
vi.mock("./AuthContext", () => ({
  useAuth: () => ({
    refresh: vi.fn(),
    user: {
      display_name: "舰队管理员",
      role: "admin",
      status: "active",
      user_id: "user-1",
      username: "admin",
    },
  }),
}));

const user = {
  display_name: "舰队管理员",
  keys: [],
  must_change_password: false,
  role: "admin",
  status: "active",
  user_id: "user-1",
  username: "admin",
};

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

describe("split management pages", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    Object.values(apiMocks).forEach((mock) => mock.mockReset());
    apiMocks.listAdminUsers.mockResolvedValue([user]);
    apiMocks.listMyKeys.mockResolvedValue([]);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  const render = async (component: React.ReactNode) => {
    await act(async () => {
      root.render(component);
      await Promise.resolve();
    });
  };

  it("keeps user lifecycle and service keys on the user page", async () => {
    await render(<AdminUsersPage />);
    expect(container).toHaveTextContent("用户列表");
    expect(container).toHaveTextContent("有效密钥");
    expect(container).not.toHaveTextContent("允许军团");
    expect(container).not.toHaveTextContent("审计记录");
  });

  it("offers deletion for another user but not the current administrator", async () => {
    apiMocks.listAdminUsers.mockResolvedValue([
      user,
      { ...user, display_name: "侦察员", role: "member", user_id: "user-2", username: "scout" },
    ]);
    await render(<AdminUsersPage />);

    const buttons = Array.from(container.querySelectorAll("button"));
    const viewScout = buttons.find((button) => button.getAttribute("aria-label") === "查看 侦察员");
    expect(viewScout).toBeDefined();
    await act(async () => viewScout?.click());

    const actionButton = container.querySelector('[aria-label="用户操作"]') as HTMLButtonElement | null;
    await act(async () => actionButton?.click());
    expect(container).toHaveTextContent("删除用户");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const deleteButton = Array.from(container.querySelectorAll<HTMLElement>('[role="menuitem"]'))
      .find((item) => item.textContent?.includes("删除用户"));
    expect(deleteButton).toBeDefined();
    await act(async () => {
      deleteButton?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      await Promise.resolve();
    });
    expect(apiMocks.deleteUser).toHaveBeenCalledWith("user-2");
  });

  it("places user lifecycle actions in the drawer header menu", async () => {
    apiMocks.listAdminUsers.mockResolvedValue([
      user,
      { ...user, display_name: "侦察员", role: "member", user_id: "user-2", username: "scout" },
    ]);
    await render(<AdminUsersPage />);

    const viewScout = Array.from(container.querySelectorAll("button"))
      .find((button) => button.getAttribute("aria-label") === "查看 侦察员");
    await act(async () => viewScout?.click());

    const header = container.querySelector(".management-drawer-header-content");
    const body = container.querySelector(".management-drawer-body");
    expect(header?.querySelector('[aria-label="用户操作"]')).toBeInTheDocument();
    expect(body).not.toHaveTextContent("禁用用户");

    const actionButton = header?.querySelector('[aria-label="用户操作"]') as HTMLButtonElement | null;
    await act(async () => actionButton?.click());
    expect(container.querySelector('[aria-label="用户操作菜单"]')).toHaveTextContent("禁用用户");
    expect(body).not.toHaveTextContent("禁用用户");
  });

  it("does not expose local client-key issuance for a member", async () => {
    const member = {
      ...user,
      display_name: "侦察员",
      role: "member" as const,
      user_id: "user-2",
      username: "scout",
    };
    apiMocks.listAdminUsers.mockResolvedValue([user, member]);
    await render(<AdminUsersPage />);

    const viewMember = Array.from(container.querySelectorAll("button"))
      .find((button) => button.getAttribute("aria-label") === "查看 侦察员");
    await act(async () => viewMember?.click());
    const actionButton = container.querySelector(
      '[aria-label="用户操作"]',
    ) as HTMLButtonElement | null;
    await act(async () => actionButton?.click());
    expect(container.querySelector('[role="menuitem"]'))
      .not.toHaveTextContent("创建设备密钥");
    expect(container).toHaveTextContent("客户端密钥统一由 GloryNavy_Seat 签发");
  });

  it("keeps device credentials separate from password settings", async () => {
    apiMocks.listMyKeys.mockResolvedValue([{
      identity_verified: true,
      key_id: "key-1",
      key_prefix: "eve_example",
      key_type: "desktop",
      last_used_at: "2026-07-27T12:00:00Z",
      name: "主监控端",
      status: "active",
      user_id: "user-1",
    }]);
    await render(<AccountKeysPage />);
    expect(container).toHaveTextContent("客户端访问凭据");
    expect(container).toHaveTextContent("设备名称");
    expect(container).toHaveTextContent("密钥前缀");
    expect(container).toHaveTextContent("主监控端");
    expect(container).not.toHaveTextContent("当前密码");
  });

  it("shows Seat ownership instead of a local key creation form", async () => {
    await render(<AccountKeysPage />);
    expect(container).toHaveTextContent("客户端密钥统一由 GloryNavy_Seat 签发");
    expect(container.querySelector(".account-key-form")).toBeNull();
    expect(container).not.toHaveTextContent("创建设备密钥");
  });

  it("allows manually revoked keys to be enabled or deleted", async () => {
    apiMocks.listMyKeys.mockResolvedValue([{
      identity_verified: true,
      key_id: "key-2",
      key_prefix: "eve_revoked",
      key_type: "desktop",
      last_used_at: "2026-07-27T12:00:00Z",
      name: "备用监控端",
      revoked_reason: "revoked by user",
      status: "revoked",
      user_id: "user-1",
    }]);
    await render(<AccountKeysPage />);

    expect(container.querySelector('[aria-label="重新启用 备用监控端"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="删除 备用监控端"]')).toBeInTheDocument();
  });
});
