import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccountKeysPage } from "./AccountKeysPage";
import { AdminAuditPage, filterAuditRecords } from "./AdminAuditPage";
import { AdminIdentityPage } from "./AdminIdentityPage";
import { AdminUsersPage } from "./AdminUsersPage";
import { AdminWhitelistPage } from "./AdminWhitelistPage";

const apiMocks = vi.hoisted(() => ({
  addCorporation: vi.fn(),
  addWhitelistCharacter: vi.fn(),
  createMyKey: vi.fn(),
  createServiceKey: vi.fn(),
  createUser: vi.fn(),
  deleteKey: vi.fn(),
  deleteUser: vi.fn(),
  enableKey: vi.fn(),
  fetchClients: vi.fn(),
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
    apiMocks.fetchClients.mockResolvedValue({ count: 0, heartbeats: [], summary: {} });
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
    const deleteButton = Array.from(container.querySelectorAll("button"))
      .find((button) => button.textContent?.includes("删除用户"));
    await act(async () => {
      deleteButton?.click();
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

    const header = container.querySelector(".management-drawer-header");
    const body = container.querySelector(".management-drawer-body");
    expect(header?.querySelector('[aria-label="用户操作"]')).toBeInTheDocument();
    expect(body).not.toHaveTextContent("禁用用户");

    const actionButton = header?.querySelector('[aria-label="用户操作"]') as HTMLButtonElement | null;
    await act(async () => actionButton?.click());
    expect(header).toHaveTextContent("禁用用户");
    expect(body).not.toHaveTextContent("禁用用户");
  });

  it("keeps verified identities on the identity page", async () => {
    await render(<AdminIdentityPage />);
    expect(container).toHaveTextContent("身份记录");
    expect(container).toHaveTextContent("已验证身份");
    expect(container).toHaveTextContent("已验证角色");
    expect(container).not.toHaveTextContent("允许军团");
    expect(container).not.toHaveTextContent("角色白名单");
    expect(apiMocks.listCorporations).not.toHaveBeenCalled();
    expect(container).not.toHaveTextContent("创建只读服务密钥");
  });

  it("keeps corporation and character allowlists on the whitelist page", async () => {
    apiMocks.listCorporations.mockResolvedValue([{
      corporation_id: 98000001,
      corporation_name: "测试军团",
    }]);

    await render(<AdminWhitelistPage />);

    expect(container).toHaveTextContent("白名单管理");
    expect(container).toHaveTextContent("军团白名单");
    expect(container).toHaveTextContent("测试军团");
    expect(container).toHaveTextContent("ID 98000001");
    expect(container).toHaveTextContent("角色白名单");
    expect(container).not.toHaveTextContent("已验证身份");
    expect(apiMocks.listAdminUsers).toHaveBeenCalledTimes(1);
    expect(apiMocks.listCorporations).toHaveBeenCalledTimes(1);
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

  it("shows identity client exceptions with key, character and reason", async () => {
    apiMocks.listAudit.mockResolvedValue([{
      action: "identity.check_failed",
      audit_id: "audit-identity-failed",
      created_at: "2026-07-28T12:00:00Z",
      target_user_id: "user-1",
      details: {
        api_key_id: "key-1",
        api_key_name: "主监控端",
        api_key_prefix: "eve_example",
        characters: ["Unknown Pilot"],
        error_code: "identity_validation_unavailable",
        reason: "EVE identity lookup failed",
      },
    }]);

    await render(<AdminAuditPage />);

    expect(container).toHaveTextContent("身份检查异常");
    expect(container).toHaveTextContent("客户端 主监控端（eve_example）");
    expect(container).toHaveTextContent("角色 Unknown Pilot");
    expect(container).toHaveTextContent("原因 EVE 身份服务不可用");
    expect(container).not.toHaveTextContent("identity.check_failed");
  });

  it("shows only real client heartbeat exceptions", async () => {
    apiMocks.fetchClients.mockResolvedValue({
      count: 5,
      summary: {},
      heartbeats: [
        {
          client_id: "detector:error",
          label: "主监控端",
          status: "running",
          seen_at: "2026-07-28T12:10:00Z",
          details: {
            client_version: "1.0.3",
            host: "DESKTOP-01",
            last_action: "OCR 上报",
            last_error: "snapshot upload timed out",
          },
        },
        {
          client_id: "channel:failed",
          label: "频道客户端",
          status: "failed",
          seen_at: "2026-07-28T12:11:00Z",
          details: { host: "SERVER-01" },
        },
        {
          client_id: "detector:stopped",
          label: "主动停止端",
          status: "stopped",
          seen_at: "2026-07-28T12:12:00Z",
          details: { last_action: "停止监控" },
        },
        {
          client_id: "detector:pending",
          label: "等待日志端",
          status: "pending",
          seen_at: "2026-07-28T12:13:00Z",
          details: { last_action: "等待新日志 Listener" },
        },
        {
          client_id: "detector:stale-error",
          label: "离线旧异常端",
          status: "error",
          online: false,
          seen_at: "2026-07-27T12:00:00Z",
          details: { last_error: "历史连接异常" },
        },
      ],
    });

    await render(<AdminAuditPage />);

    expect(container).toHaveTextContent("客户端异常检测");
    expect(container).toHaveTextContent("2 个异常");
    expect(container).toHaveTextContent("主监控端｜DESKTOP-01 · 1.0.3");
    expect(container).toHaveTextContent("snapshot upload timed out｜最后操作 OCR 上报");
    expect(container).toHaveTextContent("频道客户端｜SERVER-01 · 未知版本");
    expect(container).toHaveTextContent("客户端状态异常");
    expect(container).not.toHaveTextContent("主动停止端");
    expect(container).not.toHaveTextContent("等待日志端");
    expect(container).not.toHaveTextContent("离线旧异常端");
  });

  it("keeps audit records visible when client status loading fails", async () => {
    apiMocks.listAudit.mockResolvedValue([{
      action: "user.created",
      audit_id: "audit-visible",
      created_at: "2026-07-28T12:00:00Z",
      target_user_id: "user-1",
    }]);
    apiMocks.fetchClients
      .mockResolvedValueOnce({
        count: 1,
        summary: {},
        heartbeats: [{
          client_id: "detector:old-error",
          label: "上次异常端",
          status: "error",
          seen_at: "2026-07-28T12:00:00Z",
          details: { last_error: "旧异常" },
        }],
      })
      .mockRejectedValueOnce(new Error("客户端状态加载失败"));

    await render(<AdminAuditPage />);

    expect(container).toHaveTextContent("创建用户");
    expect(container).toHaveTextContent("上次异常端");

    const refreshButton = Array.from(container.querySelectorAll("button"))
      .find((button) => button.textContent?.includes("刷新日志"));
    await act(async () => {
      refreshButton?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container).toHaveTextContent("客户端状态加载失败");
    expect(container).not.toHaveTextContent("上次异常端");
    expect(container).toHaveTextContent("当前异常客户端0");
  });

  it("filters audit records by client, category, period and outcome", () => {
    const now = new Date("2026-08-01T12:00:00Z").getTime();
    const records = [
      {
        action: "identity.check_failed",
        actor_user_id: "operator-1",
        audit_id: "audit-failed",
        created_at: "2026-08-01T11:00:00Z",
        details: {
          api_key_name: "主监控端",
          characters: ["Pilot Alpha"],
        },
        target_user_id: "user-1",
      },
      {
        action: "identity.verified",
        audit_id: "audit-verified",
        created_at: "2026-08-01T10:00:00Z",
        details: { characters: ["Pilot Bravo"] },
        target_user_id: "user-2",
      },
      {
        action: "api_key.created",
        audit_id: "audit-key",
        created_at: "2026-07-31T12:00:00Z",
        details: { key_id: "key-1", name: "备用客户端" },
        target_user_id: "user-3",
      },
      {
        action: "user.created",
        audit_id: "audit-old-account",
        created_at: "2026-07-01T12:00:00Z",
        target_user_id: "user-4",
      },
    ];

    expect(filterAuditRecords(records, {
      category: "all",
      outcome: "all",
      period: "all",
      search: "pilot alpha",
    }, now).map((item) => item.audit_id)).toEqual(["audit-failed"]);
    expect(filterAuditRecords(records, {
      category: "key",
      outcome: "all",
      period: "all",
      search: "",
    }, now).map((item) => item.audit_id)).toEqual(["audit-key"]);
    expect(filterAuditRecords(records, {
      category: "all",
      outcome: "exception",
      period: "7d",
      search: "",
    }, now).map((item) => item.audit_id)).toEqual(["audit-failed"]);
  });

  it("renders labeled audit filters and keeps unknown action codes visible", async () => {
    apiMocks.listAudit.mockResolvedValue([{
      action: "custom.security_event",
      actor_user_id: "operator-1",
      audit_id: "audit-custom",
      created_at: "2026-08-01T10:00:00Z",
      target_user_id: "user-1",
    }]);

    await render(<AdminAuditPage />);

    expect(container.querySelector('[aria-label="搜索客户端、角色或用户"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="操作类别"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="时间范围"]')).toBeInTheDocument();
    expect(container.querySelector('[aria-label="审计状态"]')).toBeInTheDocument();
    expect(container).toHaveTextContent("其他操作（custom.security_event）");
    expect(container).toHaveTextContent("operator-1 → user-1");
    expect(container).toHaveTextContent("筛选结果 1 条 / 已加载 1 条");
  });

  it("paginates audit records and clamps the page after a silent refresh", async () => {
    const initialRecords = Array.from({ length: 21 }, (_, index) => ({
      action: "user.created",
      audit_id: `audit-${index + 1}`,
      created_at: `2026-08-01T10:${String(index).padStart(2, "0")}:00Z`,
      target_user_id: `user-${index + 1}`,
    }));
    apiMocks.listAudit
      .mockResolvedValueOnce(initialRecords)
      .mockResolvedValueOnce([{
        action: "user.created",
        audit_id: "audit-new",
        created_at: "2026-08-01T12:00:00Z",
        target_user_id: "user-new",
      }]);
    apiMocks.fetchClients
      .mockResolvedValueOnce({
        count: 1,
        heartbeats: [{
          age_seconds: 30,
          client_id: "detector:offline",
          label: "短时离线端",
          online: false,
          seen_at: "2026-08-01T11:59:30Z",
          status: "running",
        }],
        summary: {},
      })
      .mockRejectedValueOnce(new Error("增量客户端刷新失败"));

    const intervalSpy = vi.spyOn(window, "setInterval").mockReturnValue(
      42 as unknown as ReturnType<typeof window.setInterval>,
    );

    await render(<AdminAuditPage />);
    const poll = intervalSpy.mock.calls.find(([, delay]) => delay === 15_000)?.[0] as
      | (() => void)
      | undefined;
    expect(container).toHaveTextContent("筛选结果 21 条 / 已加载 21 条");
    expect(container).not.toHaveTextContent("user-21");

    const secondPage = Array.from(container.querySelectorAll(".arco-pagination-item"))
      .find((item) => item.textContent?.trim() === "2") as HTMLElement | undefined;
    expect(secondPage).toBeDefined();
    await act(async () => secondPage?.click());
    expect(container).toHaveTextContent("user-21");

    await act(async () => {
      poll?.();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container).toHaveTextContent("user-new");
    expect(container).not.toHaveTextContent("user-21");
    expect(container).toHaveTextContent("筛选结果 1 条 / 已加载 1 条");
    expect(container).toHaveTextContent("增量客户端刷新失败");
    expect(container).toHaveTextContent("短时离线端");
    intervalSpy.mockRestore();
  });

  it("shows recent offline clients separately from stale or intentionally stopped clients", async () => {
    apiMocks.fetchClients.mockResolvedValue({
      count: 4,
      summary: {},
      heartbeats: [
        {
          age_seconds: 60,
          client_id: "detector:recent-offline",
          label: "最近离线端",
          online: false,
          seen_at: "2026-08-01T11:59:00Z",
          status: "running",
        },
        {
          age_seconds: 90_000,
          client_id: "detector:stale-offline",
          label: "历史离线端",
          online: false,
          seen_at: "2026-07-30T10:00:00Z",
          status: "error",
        },
        {
          age_seconds: 30,
          client_id: "detector:stopped",
          label: "主动停止端",
          online: false,
          seen_at: "2026-08-01T11:59:30Z",
          status: "stopped",
        },
        {
          client_id: "detector:error",
          label: "在线异常端",
          online: true,
          seen_at: "2026-08-01T12:00:00Z",
          status: "error",
        },
      ],
    });

    await render(<AdminAuditPage />);

    expect(container).toHaveTextContent("2 个异常");
    expect(container).toHaveTextContent("最近离线端");
    expect(container).toHaveTextContent("客户端心跳超时，当前离线");
    expect(container).toHaveTextContent("在线异常端");
    expect(container).not.toHaveTextContent("历史离线端");
    expect(container).not.toHaveTextContent("主动停止端");
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
