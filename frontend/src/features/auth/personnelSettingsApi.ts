import { apiRequest } from "./api";

export type PersonnelMode = "off" | "shadow" | "on";
export type PersonnelValues = {
  mode: PersonnelMode;
  background_refresh: boolean;
  history_backfill: boolean;
  background_max: number;
};
export type PersonnelSettingsSnapshot = {
  values: PersonnelValues;
  effective: PersonnelValues;
  revision: string;
  source: "environment" | "database";
  restart_required: boolean;
  apply_required?: boolean;
  available: boolean;
  unavailable_reason: string;
  writable: boolean;
};

async function request(init?: RequestInit): Promise<PersonnelSettingsSnapshot> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const result = await apiRequest<{ settings: PersonnelSettingsSnapshot }>(
      "/api/v1/admin/personnel-settings", { ...init, signal: controller.signal },
    );
    return result.settings;
  } catch (error) {
    if (controller.signal.aborted) throw new Error("请求超时，保存结果未确认；请重新读取配置后核对。");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export const fetchPersonnelSettings = () => request();
export const updatePersonnelSettings = (values: PersonnelValues, revision: string) => request({
  method: "POST", body: JSON.stringify({ values, revision }),
});
