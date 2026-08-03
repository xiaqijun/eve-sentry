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

    const paths = vi.mocked(apiRequest).mock.calls.map(([path]) => path);
    expect(paths).toHaveLength(2);
    expect(paths.map((path) => new URL(path, "http://localhost").pathname)).toEqual([
      "/api/v1/alert-history",
      "/api/v1/hostile-waves",
    ]);
    expect(paths.some((path) => path.startsWith("/api/alerts"))).toBe(false);
    paths.forEach((path) => {
      const query = new URL(path, "http://localhost").searchParams;
      expect(query.get("limit")).toBe("1000");
      expect(query.has("since")).toBe(true);
    });
  });
});
