import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../auth/api";
import { fetchHostileAlertHistory } from "./api";

vi.mock("../auth/api", () => ({
  apiRequest: vi.fn(),
}));

describe("fetchHostileAlertHistory", () => {
  beforeEach(() => {
    vi.mocked(apiRequest).mockReset();
    vi.mocked(apiRequest).mockResolvedValue({ alerts: [], count: 0 });
  });

  it("bounds the derived alert history query", async () => {
    await fetchHostileAlertHistory("7d");

    const path = vi.mocked(apiRequest).mock.calls[0][0];
    const query = new URL(path, "http://localhost").searchParams;
    expect(query.get("limit")).toBe("1000");
    expect(query.has("since")).toBe(true);
  });
});
