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
  it("keeps organization comparison separate from archive mode and saves online", async () => {
    const values = { ...initial.values, mode: "on" as const, organization_mode: "off" as const };
    mocks.fetch.mockResolvedValue({ ...initial, values, effective: values,
      organization_shadow: { compared: 12, different: 2 } });
    const updated = { ...values, organization_mode: "shadow" as const };
    mocks.update.mockResolvedValue({ ...initial, values: updated, effective: updated });
    await render();
    expect(container).toHaveTextContent("已比较 12 次，差异 2 次");
    const organization = container.querySelector('[aria-label="组织关系分类"]')!;
    await click(organization.querySelector('input[value="shadow"]')!);
    await click(button("保存配置"));
    expect(mocks.update).toHaveBeenCalledWith(updated, "environment");
    expect(container).toHaveTextContent("当前组织模式：影子运行");
    expect(container).toHaveTextContent("无需重启");
  });
  it("disables organization controls while archive is inactive", async () => {
    const values = { ...initial.values, organization_mode: "on" as const };
    mocks.fetch.mockResolvedValue({ ...initial, values });
    await render();
    const organization = container.querySelector('[aria-label="组织关系分类"]')!;
    expect(organization.querySelector('input[value="on"]')).toBeDisabled();
    expect(container).toHaveTextContent("关闭档案时保留配置但不执行");
  });

  it("separates all pending calls from valid comparisons without inventing a rate", async () => {
    const values = { ...initial.values, mode: "on" as const, organization_mode: "shadow" as const };
    mocks.fetch.mockResolvedValue({ ...initial, values, effective: values,
      organization_shadow: { compared: 2288, different: 2288, comparable: 0, pending: 2288, decision_different: 0 } });
    await render();
    expect(container).toHaveTextContent("有效比较 0 次");
    expect(container).toHaveTextContent("待确认 2288 次");
    expect(container).toHaveTextContent("敌我差异 0 次");
    expect(container).toHaveTextContent("暂无（无有效样本）");
    expect(container).not.toHaveTextContent("100.0%");
  });

  it("uses only known decisions as the rate denominator", async () => {
    const values = { ...initial.values, mode: "on" as const, organization_mode: "shadow" as const };
    mocks.fetch.mockResolvedValue({ ...initial, values, effective: values,
      organization_shadow: { compared: 10, different: 8, comparable: 4, pending: 6, decision_different: 2 } });
    await render();
    expect(container).toHaveTextContent("差异率 50.0%");
    expect(container).toHaveTextContent("非去重人数");
  });
});
