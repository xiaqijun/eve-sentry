import type { AlertItem } from "../workbench/types";
import { reportRangeStart, type ReportRange } from "./reporting";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(
  /\/$/,
  "",
) || "";

export interface HostileAlertHistory {
  alerts: AlertItem[];
  count: number;
  generatedAt: string;
}

export async function fetchHostileAlertHistory(
  range: ReportRange,
): Promise<HostileAlertHistory> {
  const query = new URLSearchParams();
  const startMs = reportRangeStart(range);
  if (startMs !== null) {
    query.set("since", new Date(startMs).toISOString());
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const response = await fetch(`${API_BASE}/api/alerts${suffix}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
  });
  const payload = await response.json() as {
    alerts?: AlertItem[];
    count?: number;
    error?: string;
  };
  if (!response.ok) {
    throw new Error(payload.error || "来袭历史加载失败");
  }
  const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
  return {
    alerts,
    count: Number(payload.count ?? alerts.length),
    generatedAt: new Date().toISOString(),
  };
}
