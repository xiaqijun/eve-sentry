import { beforeEach, describe, expect, it, vi } from "vitest";

import { connectAlerts } from "./api";

class FakeEventSource {
  static lastUrl = "";
  readonly url: string;
  readonly withCredentials = true;
  readonly close = vi.fn();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.lastUrl = url;
  }

  addEventListener(): void {
    // The test only verifies the subscription parameters.
  }
}

describe("workbench event stream", () => {
  beforeEach(() => {
    FakeEventSource.lastUrl = "";
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("requests bootstrap snapshots for realtime star-map updates", () => {
    const stream = connectAlerts(vi.fn(), "2026-08-05T09:00:00Z");
    const url = new URL(FakeEventSource.lastUrl, "http://localhost");

    expect(url.searchParams.get("bootstrap")).toBe("1");
    expect(url.searchParams.get("since")).toBe("2026-08-05T09:00:00Z");
    expect(url.searchParams.get("timeout")).toBe("30");
    stream.close();
  });
});
