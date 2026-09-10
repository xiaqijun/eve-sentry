import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PersonnelSettingsPanel } from "./PersonnelSettingsPanel";
import type { PersonnelSettingsSnapshot } from "./personnelSettingsApi";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), update: vi.fn() }));
vi.mock("./personnelSettingsApi", () => ({ fetchPersonnelSettings: mocks.fetch, updatePersonnelSettings: mocks.update }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const initial: PersonnelSettingsSnapshot = {
  values: { mode: "off", background_refresh: true, history_backfill: true, background_max: 4 },
  effective: { mode: "off", background_refresh: false, history_backfill: false, background_max: 0 },
  revision: "environment", source: "environment", restart_required: false, available: true,
  unavailable_reason: "", writable: true,
};

describe("PersonnelSettingsPanel", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  beforeEach(() => {
    container = document.createElement("div"); document.body.appendChild(container);
    root = createRoot(container); mocks.fetch.mockReset(); mocks.update.mockReset();
    mocks.fetch.mockResolvedValue(initial);
  });
  afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
  const render = async () => { await act(async () => { root.render(<PersonnelSettingsPanel />); }); };
  const button = (text: string) => Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes(text))!;
  const mode = (value: string) => container.querySelector<HTMLInputElement>(`input[type=radio][value="${value}"]`)!;
  const click = async (element: HTMLElement) => { await act(async () => element.click()); };

  it("shows real mode and disables save until changed", async () => {
    await render();
    expect(container).toHaveTextContent("当前运行：关闭");
    expect(button("保存配置")).toBeDisabled();
    await click(mode("shadow"));
    expect(button("保存配置")).not.toBeDisabled();
    expect(container).toHaveTextContent("有未保存修改");
  });
  it("shows a mode as effective only after the server confirms hot activation", async () => {
    mocks.update.mockResolvedValue({ ...initial, values: { ...initial.values, mode: "shadow" },
      effective: { ...initial.values, mode: "shadow" }, revision: "v2", source: "database" });
    await render(); await click(mode("shadow")); await click(button("保存配置"));
    expect(mocks.update).toHaveBeenCalledWith({ ...initial.values, mode: "shadow" }, "environment");
    expect(container).toHaveTextContent("当前运行：影子运行");
    expect(container).toHaveTextContent("已保存：影子运行");
    expect(container).toHaveTextContent("配置已保存并生效，无需重启");
    expect(button("保存配置")).toBeDisabled();
  });
  it("permits retrying saved but unapplied configuration without editing", async () => {
    const saved = { ...initial, values: { ...initial.values, mode: "on" as const }, apply_required: true };
    mocks.fetch.mockResolvedValue(saved);
    mocks.update.mockResolvedValue({ ...saved, effective: saved.values, apply_required: false });
    await render();
    expect(button("重试应用")).not.toBeDisabled();
    expect(container).toHaveTextContent("当前运行：关闭");
    await click(button("重试应用"));
    expect(container).toHaveTextContent("当前运行：正式启用");
    expect(button("保存配置")).toBeDisabled();
  });
  it("retains a failed draft and permits discard and reload after a conflict", async () => {
    mocks.update.mockRejectedValue(new Error("配置已被其他管理员修改，请重新读取后保存"));
    await render(); await click(mode("on")); await click(button("保存配置"));
    expect(mode("on")).toBeChecked();
    expect(container.querySelector('[role="alert"]')).toHaveTextContent("其他管理员");
    expect(button("重新读取配置")).toBeDisabled();
    await click(button("撤销未保存修改"));
    expect(mode("off")).toBeChecked();
    await click(button("重新读取配置"));
    expect(mocks.fetch).toHaveBeenCalledTimes(2);
  });
  it("locks controls during save and does not duplicate submissions", async () => {
    let finish!: (value: PersonnelSettingsSnapshot) => void;
    mocks.update.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await render(); await click(mode("shadow")); await click(button("保存配置"));
    expect(mode("on")).toBeDisabled();
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(mocks.update).toHaveBeenCalledTimes(1);
    await act(async () => finish(initial));
  });
  it("does not invent configuration when loading fails", async () => {
    mocks.fetch.mockRejectedValue(new Error("服务端尚未升级"));
    await render(); expect(container).toHaveTextContent("配置未加载");
    expect(container.querySelector("form")).toBeNull();
    mocks.fetch.mockResolvedValue(initial); await click(button("重新读取配置"));
    expect(container).toHaveTextContent("当前运行：关闭");
  });
  it("disables unavailable modes and switches background settings explicitly", async () => {
    mocks.fetch.mockResolvedValue({ ...initial, available: false, unavailable_reason: "需要 PostgreSQL 存储并启用 ESI" });
    await render(); expect(mode("on")).toBeDisabled(); expect(mode("shadow")).toBeDisabled();
    await click(container.querySelector('[aria-label="闲时资料刷新"]')!);
    expect(container.querySelector('input[type=radio][value="1"]')).toBeDisabled();
    await click(button("撤销未保存修改"));
    expect(container.querySelector('input[type=radio][value="1"]')).not.toBeDisabled();
  });
});
