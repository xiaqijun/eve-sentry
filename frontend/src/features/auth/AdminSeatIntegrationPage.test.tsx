import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminSeatIntegrationPage } from "./AdminSeatIntegrationPage";

const api = vi.hoisted(() => ({
  apiPath: (path: string) => path,
  fetchSeatIntegration: vi.fn(),
  updateSeatIntegration: vi.fn(),
  saveSeatServiceToken: vi.fn(),
}));
vi.mock("./api", () => api);

const standalone = {
  enabled: false,
  local_issuance: true,
  token_configured: true,
  revision: 2,
  editable: true,
  last_authenticated_at: null,
};

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

describe("SeAT integration page", () => {
  let element: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    element = document.createElement("div");
    document.body.appendChild(element);
    root = createRoot(element);
    api.fetchSeatIntegration.mockReset();
    api.updateSeatIntegration.mockReset();
    api.saveSeatServiceToken.mockReset();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    element.remove();
  });

  async function render() {
    await act(async () => root.render(<AdminSeatIntegrationPage />));
    const toggle = element.querySelector<HTMLButtonElement>('[aria-label="启用 SeAT"]')!;
    const save = Array.from(element.querySelectorAll("button")).find((button) => button.textContent === "保存模式")!;
    return { toggle, save };
  }

  it("shows setup steps and saves the selected mode with the loaded revision", async () => {
    api.fetchSeatIntegration.mockResolvedValue(standalone);
    api.updateSeatIntegration.mockResolvedValue({ ...standalone, enabled: true, local_issuance: false, revision: 3 });
    const { toggle, save } = await render();
    expect(element).toHaveTextContent("" + window.location.origin);
    expect(save).toBeDisabled();
    await act(async () => toggle.click());
    expect(save).toBeEnabled();
    expect(api.updateSeatIntegration).not.toHaveBeenCalled();
    await act(async () => save.click());
    expect(api.updateSeatIntegration).toHaveBeenCalledWith(true, 2);
    expect(element.querySelector('[role="status"]')).toHaveTextContent("已切换至 SeAT 版");
  });

  it("generates a service token and displays it once after saving", async () => {
    const generated = "f".repeat(64);
    api.fetchSeatIntegration.mockResolvedValue({ ...standalone, token_configured: false });
    api.saveSeatServiceToken.mockResolvedValue({ settings: standalone, service_token: generated });
    await render();
    expect(element.querySelector("details")).not.toHaveAttribute("open");
    expect(element.querySelector('[aria-label="复制令牌"]')).toBeNull();
    const button = Array.from(element.querySelectorAll("button")).find((item) => item.textContent === "生成并保存新令牌")!;
    await act(async () => button.click());
    expect(api.saveSeatServiceToken).toHaveBeenCalledWith(2, undefined);
    expect(element).toHaveTextContent(generated);
    expect(element.querySelector('[aria-label="复制令牌"]')).toHaveTextContent("复制令牌");
    const hide = Array.from(element.querySelectorAll("button")).find((item) => item.textContent === "隐藏令牌")!;
    await act(async () => hide.click());
    expect(element).not.toHaveTextContent(generated);
    expect(element.querySelector('[aria-label="复制令牌"]')).toBeNull();
  });

  it("shows the missing token and prevents enabling before setup", async () => {
    api.fetchSeatIntegration.mockResolvedValue({ ...standalone, token_configured: false });
    const { toggle, save } = await render();
    await act(async () => toggle.click());
    expect(element).toHaveTextContent("请先生成或保存服务令牌");
    expect(save).toBeDisabled();
  });
});
