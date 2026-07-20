import type { BootstrapPayload, WorkbenchSummary } from "./types";

export function summarizeWorkbench(
  bootstrap: BootstrapPayload,
): WorkbenchSummary {
  return {
    systems: Number(bootstrap.map.summary.system_count ?? bootstrap.map.systems.length),
    hostiles: Number(bootstrap.map.summary.hostile_count ?? 0),
    reports: Number(bootstrap.map.summary.report_count ?? bootstrap.reports.length),
    alerts: Number(bootstrap.map.summary.alert_count ?? bootstrap.alerts.length),
    onlineClients: Number(bootstrap.clients.summary.online_count ?? 0),
  };
}
