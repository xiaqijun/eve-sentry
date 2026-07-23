import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  Clock3,
  MapPinned,
  RefreshCw,
  ShieldAlert,
  Skull,
  Users,
} from "lucide-react";
import { Link } from "react-router-dom";

import { fetchHostileAlertHistory } from "./api";
import {
  buildHostileReport,
  type ReportRange,
  type SeverityReportRow,
} from "./reporting";
import type { Level } from "../workbench/types";

const REPORT_REFRESH_INTERVAL_MS = 60000;

const RANGE_OPTIONS: Array<{ value: ReportRange; label: string }> = [
  { value: "24h", label: "24 小时" },
  { value: "7d", label: "7 天" },
  { value: "30d", label: "30 天" },
  { value: "all", label: "全部" },
];

function formatTime(value?: string): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function levelLabel(level?: Level | "unknown"): string {
  const labels: Record<Level | "unknown", string> = {
    critical: "严重",
    high: "高危",
    medium: "中危",
    low: "低危",
    unknown: "未知",
  };
  return labels[level || "unknown"];
}

function severityClass(level?: Level | "unknown"): string {
  return `severity-${level || "unknown"}`;
}

function SeverityBars({ rows }: { rows: SeverityReportRow[] }) {
  const maximum = Math.max(1, ...rows.map((row) => row.count));
  return (
    <div className="severity-bars">
      {rows.map((row) => (
        <div className="severity-row" key={row.level}>
          <span>{levelLabel(row.level)}</span>
          <div className="severity-track">
            <i
              className={severityClass(row.level)}
              style={{ width: `${(row.count / maximum) * 100}%` }}
            />
          </div>
          <strong>{row.count}</strong>
        </div>
      ))}
    </div>
  );
}

export function HostileReportPage() {
  const [range, setRange] = useState<ReportRange>("7d");
  const historyQuery = useQuery({
    queryKey: ["hostile-alert-history", range],
    queryFn: () => fetchHostileAlertHistory(range),
    refetchInterval: REPORT_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const report = useMemo(
    () => buildHostileReport(historyQuery.data?.alerts || [], range),
    [historyQuery.data?.alerts, range],
  );
  const trendMaximum = Math.max(1, ...report.trend.map((point) => point.count));
  const activeRange = RANGE_OPTIONS.find((item) => item.value === range)?.label || "7 天";

  return (
    <main className="report-shell">
      <header className="report-header">
        <div className="report-heading">
          <Link className="report-back-link" to="/">
            <ArrowLeft size={16} />
            返回态势图
          </Link>
          <div>
            <p className="eyebrow">EVE 哨兵 · 敌对情报</p>
            <h1>敌对来袭报表</h1>
            <span>所有统计仅包含 ESI 已确认存在的角色，OCR 噪声与未验证目标不计入。</span>
          </div>
        </div>
        <div className="report-header-actions">
          <div className="report-range-tabs" aria-label="报表统计范围">
            {RANGE_OPTIONS.map((item) => (
              <button
                aria-pressed={range === item.value}
                className={range === item.value ? "active" : ""}
                key={item.value}
                type="button"
                onClick={() => setRange(item.value)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button
            className="report-refresh-button"
            disabled={historyQuery.isFetching}
            type="button"
            onClick={() => void historyQuery.refetch()}
          >
            <RefreshCw className={historyQuery.isFetching ? "is-spinning" : ""} size={15} />
            刷新
          </button>
        </div>
      </header>

      {historyQuery.isError ? (
        <section className="report-message report-message-error" role="alert">
          <AlertTriangle size={18} />
          {historyQuery.error instanceof Error
            ? historyQuery.error.message
            : "来袭历史加载失败"}
        </section>
      ) : null}

      <section className="report-metrics" aria-label="敌对来袭摘要">
        <article className="report-metric-card danger">
          <span><ShieldAlert size={15} />来袭批次</span>
          <strong>{report.incidentCount}</strong>
          <small>{activeRange}内服务端告警</small>
        </article>
        <article className="report-metric-card">
          <span><Users size={15} />目标人次</span>
          <strong>{report.targetSightings}</strong>
          <small>ESI 已确认角色人次</small>
        </article>
        <article className="report-metric-card">
          <span><Skull size={15} />独立敌对</span>
          <strong>{report.uniqueTargets}</strong>
          <small>去重后的角色数量</small>
        </article>
        <article className="report-metric-card">
          <span><MapPinned size={15} />涉及星系</span>
          <strong>{report.systemCount}</strong>
          <small>{report.systems[0]?.name ? `最多：${report.systems[0].name}` : "暂无记录"}</small>
        </article>
        <article className="report-metric-card warning">
          <span><Activity size={15} />高危批次</span>
          <strong>{report.highRiskCount}</strong>
          <small>严重与高危告警</small>
        </article>
        <article className="report-metric-card">
          <span><Clock3 size={15} />日均来袭</span>
          <strong>{report.averagePerDay.toFixed(1)}</strong>
          <small>按当前统计范围折算</small>
        </article>
      </section>

      <section className="report-grid report-grid-top">
        <article className="report-panel report-trend-panel">
          <div className="report-panel-title">
            <div><BarChart3 size={17} /><span>来袭趋势</span></div>
            <strong>{report.incidentCount} 批</strong>
          </div>
          {report.trend.length > 0 ? (
            <div className="report-trend-chart" role="img" aria-label="敌对来袭时间趋势">
              {report.trend.map((point) => (
                <div className="report-trend-column" key={point.key} title={`${point.label}：${point.count} 批`}>
                  <strong>{point.count || ""}</strong>
                  <div className="report-trend-track">
                    <i style={{ height: `${Math.max(point.count ? 8 : 0, (point.count / trendMaximum) * 100)}%` }} />
                  </div>
                  <span>{point.label}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="report-empty">当前范围暂无趋势数据</div>
          )}
        </article>

        <article className="report-panel">
          <div className="report-panel-title">
            <div><ShieldAlert size={17} /><span>风险等级</span></div>
            <strong>{report.highRiskCount} 高危</strong>
          </div>
          <SeverityBars rows={report.severity} />
        </article>
      </section>

      <section className="report-grid report-grid-rankings">
        <article className="report-panel">
          <div className="report-panel-title">
            <div><MapPinned size={17} /><span>星系来袭排行</span></div>
            <strong>TOP 8</strong>
          </div>
          <div className="report-ranking-table">
            <div className="report-table-head report-system-row">
              <span>星系</span><span>批次</span><span>目标人次</span><span>独立目标</span><span>最后来袭</span>
            </div>
            {report.systems.slice(0, 8).map((item, index) => (
              <div className="report-table-row report-system-row" key={item.name}>
                <strong><b>{index + 1}</b>{item.name}</strong>
                <span>{item.incidentCount}</span>
                <span>{item.targetSightings}</span>
                <span>{item.uniqueTargets}</span>
                <time>{formatTime(item.lastSeen)}</time>
              </div>
            ))}
            {report.systems.length === 0 ? <div className="report-empty">暂无星系记录</div> : null}
          </div>
        </article>

        <article className="report-panel">
          <div className="report-panel-title">
            <div><Skull size={17} /><span>高频敌对目标</span></div>
            <strong>TOP 8</strong>
          </div>
          <div className="report-ranking-table">
            <div className="report-table-head report-target-row">
              <span>目标</span><span>出现批次</span><span>涉及星系</span><span>最后出现</span>
            </div>
            {report.targets.slice(0, 8).map((item, index) => (
              <div className="report-table-row report-target-row" key={item.name}>
                <strong><b>{index + 1}</b>{item.name}</strong>
                <span>{item.incidentCount}</span>
                <span title={item.systems.join("、")}>{item.systems.join("、")}</span>
                <time>{formatTime(item.lastSeen)}</time>
              </div>
            ))}
            {report.targets.length === 0 ? <div className="report-empty">暂无敌对目标</div> : null}
          </div>
        </article>
      </section>

      <section className="report-panel report-recent-panel">
        <div className="report-panel-title">
          <div><AlertTriangle size={17} /><span>最近来袭记录</span></div>
          <strong>更新于 {formatTime(historyQuery.data?.generatedAt)}</strong>
        </div>
        <div className="report-recent-table">
          <div className="report-table-head report-recent-row">
            <span>时间</span><span>星系</span><span>敌对目标</span><span>等级</span><span>状态</span>
          </div>
          {report.recent.map((item) => (
            <div className="report-table-row report-recent-row" key={item.id}>
              <time>{formatTime(item.created_at)}</time>
              <strong>{item.system_name || "未知星系"}</strong>
              <span title={(item.names || []).join("、")}>{(item.names || []).join("、") || "未知目标"}</span>
              <em className={severityClass(item.level)}>{levelLabel(item.level)}</em>
              <span>{item.acknowledged ? "已确认" : "未确认"}</span>
            </div>
          ))}
          {!historyQuery.isLoading && report.recent.length === 0 ? (
            <div className="report-empty">当前范围暂无敌对来袭记录</div>
          ) : null}
          {historyQuery.isLoading ? <div className="report-empty">正在加载来袭历史…</div> : null}
        </div>
      </section>
    </main>
  );
}
