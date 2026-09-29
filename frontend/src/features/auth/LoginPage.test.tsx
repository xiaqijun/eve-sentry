import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
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

  it("shows local account credentials without an EVE login link", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={[{ pathname: "/login", state: { from: { pathname: "/reports" } } }]}>
          <LoginPage />
        </MemoryRouter>,
      );
    });

    expect(container.querySelector(".esi-member-login")).not.toBeInTheDocument();
    expect(container).toHaveTextContent("平台账号");
    expect(container).toHaveTextContent("登录");
    expect(container.querySelector('input[autocomplete="username"]')).toBeInTheDocument();
  });

  it("shows local account login failures", async () => {
    loginMock.mockRejectedValueOnce(new Error("用户名或密码错误"));
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/login"]}>
          <LoginPage />
        </MemoryRouter>
      );
    });

    await act(async () => {
      container.querySelector<HTMLFormElement>("form")?.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    });

    expect(container).toHaveTextContent("用户名或密码错误");
  });
});
