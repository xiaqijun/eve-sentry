import { expect, it } from "vitest";
import { filterAuditRecords } from "./AdminAuditPage";

it("searches personnel setting changes using readable labels and values", () => {
  const record = { audit_id: "personnel-change", actor_user_id: "admin", target_user_id: "admin",
    action: "personnel.settings_changed", created_at: new Date().toISOString(),
    details: { previous: { mode: "off", background_refresh: true }, values: { mode: "shadow", background_refresh: false } } };
  for (const search of ["修改人员档案配置", "影子运行", "闲时刷新：开启 → 暂停"]) {
    expect(filterAuditRecords([record], { search, category: "all", period: "all", outcome: "all" })).toEqual([record]);
  }
});
