import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchBootstrap } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("bootstrap monitoring scope", () => {
  it("preserves the scope version for SSE configuration reconciliation", async () => {
    const monitoring_scope = { enabled: true, version: "scope-v2", systems: [{ name: "S-KSWL", system_id: 30003615 }] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ bootstrap: { monitoring_scope } }),
    }));
    expect((await fetchBootstrap()).monitoring_scope).toEqual(monitoring_scope);
  });

  it("keeps compatibility with servers that do not send a scope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ bootstrap: {} }) }));
    expect((await fetchBootstrap()).monitoring_scope).toBeUndefined();
  });
});
