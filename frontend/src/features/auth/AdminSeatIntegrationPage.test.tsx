import "@testing-library/jest-dom/vitest";
import { Message } from "@arco-design/web-react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminSeatIntegrationPage } from "./AdminSeatIntegrationPage";

const api = vi.hoisted(() => ({
  apiPath: (path: string) => path,
  fetchCurrentSeatServiceToken: vi.fn(),
  fetchSeatIntegration: vi.fn(),
  updateSeatIntegration: vi.fn(),
  saveSeatServiceToken: vi.fn(),
}));
const clipboard = vi.hoisted(() => vi.fn());
vi.mock("./api", () => api);
vi.mock("@arco-design/web-react/es/_util/clipboard", () => ({ default: clipboard }));

const standalone = {
  enabled: false,
  local_issuance: true,
  token_configured: true,
  token_copy_available: true,
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
    api.fetchCurrentSeatServiceToken.mockReset();
    api.updateSeatIntegration.mockReset();
    api.saveSeatServiceToken.mockReset();
    clipboard.mockReset().mockResolvedValue(undefined);
  });

  afterEach(async () => {
    await act(async () => {
      Message.clear();
      root.unmount();
    });
    element.remove();
  });

  async function render() {
    await act(async () => root.render(<AdminSeatIntegrationPage />));
    const toggle = element.querySelector<HTMLButtonElement>('[aria-label="启用 SeAT"]')!;
    const save = Array.from(element.querySelectorAll("button")).find((button) => button.textContent === "保存模式")!;
    return { toggle, save };
  }

  it("shows the Sentry address and saves the selected mode with the loaded revision", async () => {
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
    expect(document.body.querySelector('.arco-message[role="alert"]')).toHaveTextContent("已切换至 SeAT 版");
  });

  it("copies the current token without generating a replacement", async () => {
    const token = "f".repeat(64);
    api.fetchSeatIntegration.mockResolvedValue(standalone);
    api.fetchCurrentSeatServiceToken.mockResolvedValue(token);
    await render();
    const buttons = Array.from(element.querySelectorAll("button"));
    const copy = buttons.find((button) => button.textContent === "复制令牌")!;
    const update = buttons.find((button) => button.textContent === "更新令牌")!;
    const refresh = buttons.find((button) => button.textContent === "刷新状态")!;
    expect(update.closest(".arco-card")?.querySelectorAll("button")).toHaveLength(2);
    expect(element).not.toHaveTextContent("导入现有令牌");
    expect(element.querySelector('[aria-label="已隐藏的服务令牌"]')).toHaveTextContent("••••••••");
    expect(copy).toBeEnabled();
    expect(element).not.toHaveTextContent(token);
    expect(api.fetchCurrentSeatServiceToken).not.toHaveBeenCalled();
    await act(async () => copy.click());
    expect(clipboard).toHaveBeenCalledWith(token);
    expect(document.body.querySelector('.arco-message[role="alert"]')).toHaveTextContent("令牌已复制");
    expect(element).not.toHaveTextContent("令牌已复制");
    expect(element).not.toHaveTextContent(token);
    expect(api.saveSeatServiceToken).not.toHaveBeenCalled();
    await act(async () => refresh.click());
    expect(copy).toBeEnabled();
    await act(async () => copy.click());
    expect(api.fetchCurrentSeatServiceToken).toHaveBeenCalledTimes(2);
  });

  it("shows when a legacy token is pending recovery and enables copy after refreshing", async () => {
    api.fetchSeatIntegration.mockResolvedValueOnce({ ...standalone, token_copy_available: false });
    api.fetchSeatIntegration.mockResolvedValueOnce(standalone);
    await render();
    const buttons = Array.from(element.querySelectorAll("button"));
    const copy = buttons.find((button) => button.textContent === "复制令牌")!;
    const refresh = buttons.find((button) => button.textContent === "刷新状态")!;
    expect(element).toHaveTextContent("待恢复");
    expect(copy).toBeDisabled();
    await act(async () => refresh.click());
    expect(element).not.toHaveTextContent("待恢复");
    expect(copy).toBeEnabled();
  });

  it("shows the missing token and prevents enabling before setup", async () => {
    api.fetchSeatIntegration.mockResolvedValue({ ...standalone, token_configured: false, token_copy_available: false });
    const { toggle, save } = await render();
    await act(async () => toggle.click());
    expect(element).toHaveTextContent("请先更新令牌");
    expect(save).toBeDisabled();
  });
});
