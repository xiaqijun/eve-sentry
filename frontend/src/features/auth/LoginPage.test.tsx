import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

vi.mock("./AuthContext", () => ({
  useAuth: () => ({
    authEnabled: true,
    loading: false,
    login: vi.fn(),
    user: null,
  }),
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

describe("login page", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
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
});
