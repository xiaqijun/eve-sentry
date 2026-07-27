import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, login, setCsrfToken } from "./api";

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
});
