import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { THEME_STORAGE_KEY, ThemeProvider, useTheme } from "./ThemeContext";
import { ThemeToggle } from "./ThemeToggle";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

function ThemeProbe() {
  const { theme } = useTheme();
  return <span data-testid="theme">{theme}</span>;
}

describe("ThemeProvider", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const matchMedia = window.matchMedia;

  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    document.body.removeAttribute("data-theme");
    document.body.removeAttribute("arco-theme");
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: false }));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.stubGlobal("matchMedia", matchMedia);
  });

  it("restores a saved theme and synchronizes the document", async () => {
    localStorage.setItem(THEME_STORAGE_KEY, "dark");
    await act(async () => root.render(<ThemeProvider><ThemeProbe /></ThemeProvider>));

    expect(container).toHaveTextContent("dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.body).toHaveAttribute("data-theme", "dark");
    expect(document.body).toHaveAttribute("arco-theme", "dark");
  });

  it("follows the system preference when no saved value exists", async () => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    await act(async () => root.render(<ThemeProvider><ThemeProbe /></ThemeProvider>));
    expect(container).toHaveTextContent("dark");
  });

  it("toggles without a reload and persists the selection", async () => {
    await act(async () => root.render(<ThemeProvider><ThemeToggle /></ThemeProvider>));
    const button = container.querySelector("button") as HTMLButtonElement;
    expect(button).toHaveAttribute("aria-pressed", "false");

    await act(async () => button.click());
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.body).toHaveAttribute("arco-theme", "dark");
  });
});
