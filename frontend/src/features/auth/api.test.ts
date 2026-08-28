import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiRequest,
  createAdminKey,
  deleteKey,
  enableKey,
  fetchSecuritySettings,
  fetchEsiGateway,
  listAdminClients,
  listAdminUsers,
  listAudit,
  listCorporations,
  listMyKeys,
  login,
  setCsrfToken,
  updateSecuritySettings,
} from "./api";

describe("authenticated API client", () => {
  beforeEach(() => {
    setCsrfToken("");
    vi.restoreAllMocks();
  });

  it("stores login CSRF and includes cookie credentials on mutations", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        user: {
          user_id: "u1",
          username: "pilot",
          display_name: "Pilot",
          role: "member",
          status: "active",
          must_change_password: false,
        },
        csrf_token: "csrf-123",
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));

    await login("pilot", "password-1234");
    await apiRequest("/api/v1/me/keys", {
      method: "POST",
      body: JSON.stringify({ name: "Desktop" }),
    });

    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      credentials: "include",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-123" }),
    });
  });

  it("notifies the route guard when a session is rejected", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({
        error: "authentication is required",
        code: "authentication_required",
      }), { status: 401 }),
    );
    const listener = vi.fn();
    window.addEventListener("eve-sentry-auth-required", listener);

    await expect(apiRequest("/api/v1/bootstrap")).rejects.toMatchObject({
      status: 401,
      code: "authentication_required",
    });
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener("eve-sentry-auth-required", listener);
  });

  it("uses distinct endpoints for enabling and permanently deleting a key", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );

    await enableKey("key/1");
    await deleteKey("key/1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/me/keys/key%2F1/enable");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/me/keys/key%2F1/record");
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "DELETE" });
  });

  it("loads the dedicated administrator client inventory", async () => {
    const payload = {
      clients: { count: 0, heartbeats: [] },
      keys: [],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );

    await expect(listAdminClients()).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/clients",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("loads ESI gateway observability for administrators", async () => {
    const payload = {
      gateway: { configured: true, reachable: true },
      client_metrics: { counts: { "get_system:miss:remote": 2 } },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 200 }),
    );

    await expect(fetchEsiGateway()).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/admin/esi-gateway",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("creates a desktop key for a selected user", async () => {
    const created = {
      key_id: "key-1",
      user_id: "user/1",
      name: "监控客户端",
      key_prefix: "eve_example",
      key_type: "desktop" as const,
      status: "active" as const,
      identity_verified: true,
      created_at: "2026-08-07T00:00:00Z",
      secret: "eve_secret",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ key: created }), { status: 201 }),
    );

    await expect(
      createAdminKey("user/1", "监控客户端", "desktop"),
    ).resolves.toEqual(created);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/admin/users/user%2F1/keys",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      name: "监控客户端",
      key_type: "desktop",
    });
  });

  it("loads and updates administrator security settings", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        settings: { key_risk_control: true },
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        settings: { key_risk_control: false },
      }), { status: 200 }));

    await expect(fetchSecuritySettings()).resolves.toEqual({ key_risk_control: true });
    await expect(updateSecuritySettings({ key_risk_control: false }))
      .resolves.toEqual({ key_risk_control: false });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/admin/security-settings");
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "POST" });
  });

  it("normalizes partial collection responses at the API boundary", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ users: [{ user_id: "u1", username: "pilot" }] }), { status: 200 }),
    );
    await expect(listAdminUsers()).resolves.toEqual([expect.objectContaining({
      user_id: "u1",
      keys: [],
      whitelist: [],
      verified_characters: [],
    })]);

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 }),
    );
    await expect(listMyKeys()).resolves.toEqual([]);
    await expect(listCorporations()).resolves.toEqual([]);
    await expect(listAudit()).resolves.toEqual([]);
  });

  it("normalizes a partial client inventory", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ clients: {}, keys: null }), { status: 200 }),
    );
    await expect(listAdminClients()).resolves.toEqual({
      clients: { count: 0, heartbeats: [] },
      keys: [],
    });
  });
});
