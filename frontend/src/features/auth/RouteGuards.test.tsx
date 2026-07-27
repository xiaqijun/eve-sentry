import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProtectedRoute } from "./RouteGuards";

const useAuthMock = vi.fn();

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("./AuthContext", () => ({
  useAuth: () => useAuthMock(),
}));

describe("ProtectedRoute", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    useAuthMock.mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  it("keeps the workbench public while server authentication is disabled", async () => {
    useAuthMock.mockReturnValue({
      authEnabled: false,
      loading: false,
      user: null,
    });

    await act(async () => {
      root.render(
        <MemoryRouter>
          <ProtectedRoute allowWhenAuthDisabled>
            <div>workbench</div>
          </ProtectedRoute>
        </MemoryRouter>,
      );
    });

    expect(container.textContent).toContain("workbench");
  });

  it("requires a session once server authentication is enabled", async () => {
    useAuthMock.mockReturnValue({
      authEnabled: true,
      loading: false,
      user: null,
    });

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/reports"]}>
          <Routes>
            <Route
              path="/reports"
              element={
                <ProtectedRoute allowWhenAuthDisabled>
                  <div>report</div>
                </ProtectedRoute>
              }
            />
            <Route path="/login" element={<div>login</div>} />
          </Routes>
        </MemoryRouter>,
      );
    });

    expect(container.textContent).not.toContain("report");
    expect(container.textContent).toContain("login");
  });
});
