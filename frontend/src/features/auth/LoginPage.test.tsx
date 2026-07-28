import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

const loginMock = vi.fn();

vi.mock("./AuthContext", () => ({
  useAuth: () => ({
    authEnabled: true,
    loading: false,
    login: loginMock,
    user: null,
  }),
}));

function LoginPageHarness() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <>
      <button data-testid="clear-query" onClick={() => navigate("/login")} type="button">
        清除地址参数
      </button>
      <span data-testid="location-search">{location.search}</span>
      <LoginPage />
    </>
  );
}

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

describe("login page", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    loginMock.mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("uses EVE login for members and separates administrator credentials", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: { pathname: "/reports" } } }]}>
          <LoginPage />
        </MemoryRouter>,
      );
    });

    const esiLink = container.querySelector<HTMLAnchorElement>(".esi-member-login");
    expect(esiLink).toHaveTextContent("使用 EVE Online 登录");
    expect(esiLink?.getAttribute("href")).toContain("return_to=%2Freports");
    expect(container).toHaveTextContent("管理员登录");
    expect(container.querySelector('input[autocomplete="username"]')).toBeInTheDocument();
  });

  it("explains when the EVE character corporation is not allowed", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/login?esi_error=eve_corporation_not_allowed"]}>
          <LoginPage />
        </MemoryRouter>,
      );
    });

    expect(container).toHaveTextContent("该 EVE 角色不在允许登录的军团中");
  });

  it("clears a stale ESI error when the login URL no longer contains it", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/login?esi_error=user_disabled"]}>
          <LoginPageHarness />
        </MemoryRouter>,
      );
    });

    expect(container).toHaveTextContent("账号已被禁用");

    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-testid="clear-query"]')?.click();
    });

    expect(container).not.toHaveTextContent("账号已被禁用");
  });

  it("removes a stale ESI error before an administrator login attempt", async () => {
    loginMock.mockRejectedValueOnce(new Error("用户名或密码错误"));
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/login?esi_error=user_disabled"]}>
          <LoginPageHarness />
        </MemoryRouter>,
      );
    });

    await act(async () => {
      container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    expect(container.querySelector('[data-testid="location-search"]')).toHaveTextContent("");
    expect(container).not.toHaveTextContent("账号已被禁用");
    expect(container).toHaveTextContent("用户名或密码错误");
  });
});
