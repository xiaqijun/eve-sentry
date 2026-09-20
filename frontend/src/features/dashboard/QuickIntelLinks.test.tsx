import "@testing-library/jest-dom/vitest";
import { act, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { PilotLink, SnapshotTime, SystemLink } from "./QuickIntelLinks";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: ReturnType<typeof createRoot>;
beforeEach(() => { container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
async function render(node: ReactNode) { await act(async () => root.render(node)); }
describe("quick intel links", () => {
  it("encodes system names and leaves unknown locations unlinked", async () => {
    await render(<><SystemLink name="S-KSWL" /><SystemLink name="未知星系" /></>);
    expect(container.querySelector("a")).toHaveAttribute("href", "/?system=S-KSWL");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });
  it("only links verified positive character IDs and announces the external window", async () => {
    await render(<><PilotLink name="Alice" id={101} /><PilotLink name="Unresolved" id={0} /><PilotLink name="Invalid" id={-1} /></>);
    expect(container.querySelector("a")).toHaveAttribute("href", "https://zkillboard.com/character/101/");
    expect(container.querySelector("a")).toHaveAttribute("aria-label", "Alice · zKill（新窗口）");
    expect(container.querySelector("a")).toHaveAttribute("rel", "noopener noreferrer");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });
  it("keeps the exact timestamp accessible alongside relative time", async () => {
    await render(<SnapshotTime value="2026-09-20T03:00:00Z" now={Date.parse("2026-09-20T03:05:00Z")} />);
    expect(container.querySelector("time")).toHaveTextContent("5 分钟前");
    expect(container.querySelector("time")).toHaveAttribute("datetime", "2026-09-20T03:00:00Z");
    expect(container.querySelector("time")).toHaveAttribute("tabindex", "0");
  });
});
