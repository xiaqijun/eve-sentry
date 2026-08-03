import type { AlertItem } from "../workbench/types";
import {
  reportRangeStart,
  type HostileWaveLifecycle,
  type ReportRange,
} from "./reporting";
import { apiRequest } from "../auth/api";

export interface HostileAlertHistory {
  alerts: AlertItem[];
  waves: HostileWaveLifecycle[];
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
  const [alertPayload, wavePayload] = await Promise.all([
    apiRequest<{
      alerts?: AlertItem[];
      count?: number;
      error?: string;
    }>(`/api/alerts${suffix}`),
    apiRequest<{
      waves?: HostileWaveLifecycle[];
      count?: number;
      generated_at?: string;
      error?: string;
    }>(`/api/v1/hostile-waves${suffix}`),
  ]);
  const alerts = Array.isArray(alertPayload.alerts) ? alertPayload.alerts : [];
  const waves = Array.isArray(wavePayload.waves) ? wavePayload.waves : [];
  return {
    alerts,
    waves,
    count: Number(alertPayload.count ?? alerts.length),
    generatedAt: String(wavePayload.generated_at || new Date().toISOString()),
  };
}
