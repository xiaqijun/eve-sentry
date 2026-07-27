import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountKeysPage } from "./AccountKeysPage";
import { AccountSecurityPage } from "./AccountSecurityPage";
import { AdminAuditPage } from "./AdminAuditPage";
import { AdminIdentityPage } from "./AdminIdentityPage";
import { AdminUsersPage } from "./AdminUsersPage";

const apiMocks = vi.hoisted(() => ({
  addCorporation: vi.fn(),
  addWhitelistCharacter: vi.fn(),
  changePassword: vi.fn(),
  createMyKey: vi.fn(),
  createServiceKey: vi.fn(),
  createUser: vi.fn(),
  listAdminUsers: vi.fn(),
  listAudit: vi.fn(),
  listCorporations: vi.fn(),
  listMyKeys: vi.fn(),
  removeCorporation: vi.fn(),
  removeWhitelistCharacter: vi.fn(),
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
  verified_characters: [],
  whitelist: [],
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
    apiMocks.listAudit.mockResolvedValue([]);
    apiMocks.listCorporations.mockResolvedValue([]);
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

  it("keeps EVE authorization on the identity page", async () => {
    await render(<AdminIdentityPage />);
    expect(container).toHaveTextContent("允许军团");
    expect(container).toHaveTextContent("角色白名单");
    expect(container).toHaveTextContent("已验证角色");
    expect(container).not.toHaveTextContent("创建只读服务密钥");
  });

  it("keeps audit records on a dedicated page", async () => {
    apiMocks.listAudit.mockResolvedValue([{
      action: "user.created",
      audit_id: "audit-1",
      created_at: "2026-07-27T12:00:00Z",
      target_user_id: "user-1",
    }]);
    await render(<AdminAuditPage />);
    expect(container).toHaveTextContent("操作记录");
    expect(container).toHaveTextContent("创建用户");
    expect(container).not.toHaveTextContent("user.created");
    expect(container).not.toHaveTextContent("用户目录");
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

  it("keeps password settings separate from device credentials", async () => {
    await render(<AccountSecurityPage />);
    expect(container).toHaveTextContent("修改登录密码");
    expect(container).not.toHaveTextContent("创建设备密钥");
  });
});
