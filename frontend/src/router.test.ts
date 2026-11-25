import { matchRoutes } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { router } from "./router";

describe("management routes", () => {
  it.each(["/reports", "/reports/history", "/admin/audit"])(
    "rejects direct navigation to the retired page %s",
    (path) => {
      expect(matchRoutes(router.routes, path)).toBeNull();
    },
  );

  it.each(["/", "/dashboard", "/account/keys", "/admin/users", "/admin/clients", "/admin/esi-gateway"])(
    "keeps the management page %s available",
    (path) => {
      expect(matchRoutes(router.routes, path)).not.toBeNull();
    },
  );
});
