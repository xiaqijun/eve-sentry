import type { AlertItem } from "../workbench/types";
import { reportRangeStart, type ReportRange } from "./reporting";
import { apiRequest } from "../auth/api";

export interface HostileAlertHistory {
  alerts: AlertItem[];
  count: number;
  generatedAt: string;
}

export async function fetchHostileAlertHistory(
  range: ReportRange,
): Promise<HostileAlertHistory> {
  const query = new URLSearchParams();
  query.set("limit", "1000");
  const startMs = reportRangeStart(range);
  if (startMs !== null) {
    query.set("since", new Date(startMs).toISOString());
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  const payload = await apiRequest<{
    alerts?: AlertItem[];
    count?: number;
    error?: string;
  }>(`/api/alerts${suffix}`);
  const alerts = Array.isArray(payload.alerts) ? payload.alerts : [];
  return {
    alerts,
    count: Number(payload.count ?? alerts.length),
    generatedAt: new Date().toISOString(),
  };
}
