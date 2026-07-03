import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bell,
  ChevronDown,
  ClipboardList,
  Database,
  Filter,
  Gauge,
  Home,
  Map,
  Radio,
  Settings,
  ShipWheel,
  Skull,
  type LucideIcon,
} from "lucide-react";

import { connectAlerts, fetchBootstrap } from "./api";
import { buildPilotObservations } from "./observations";
import { ObservationTable } from "./ObservationTable";
import { useWorkbenchStore } from "./store";
import { buildTacticalGraph } from "./tacticalGraph";
import { TacticalStarMap } from "./TacticalStarMap";
import { ThreatGauge } from "./ThreatGauge";
import type {
  AlertItem,
  BootstrapPayload,
  Level,
  MapSystem,
  PilotObservation,
} from "./types";
import { summarizeWorkbench } from "./workbenchSummary";

const navItems: Array<[string, LucideIcon]> = [
  ["仪表盘", Home],
  ["星图", Map],
  ["观察列表", ClipboardList],
  ["情报订阅", Radio],
  ["舰队追踪", ShipWheel],
  ["告警中心", Bell],
  ["威胁分析", Gauge],
  ["设置", Settings],
];

function formatTime(value?: string): string {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function formatClock(value?: string): string {
  if (!value) {
    return "--:--";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
  });
}

function levelLabel(level?: Level | "unknown"): string {
  switch (level) {
    case "critical":
      return "严重";
    case "high":
      return "高危";
    case "medium":
      return "中危";
    case "low":
      return "低危";
    default:
      return "未知";
  }
}

function selectedSystem(
  bootstrap: BootstrapPayload | undefined,
  selectedSystemId: number | null,
): MapSystem | null {
  if (!bootstrap || typeof selectedSystemId !== "number") {
    return null;
  }
  return bootstrap.map.systems.find((item) => item.system_id === selectedSystemId) || null;
}

function matchesObservationFilter(
  item: PilotObservation,
  filterText: string,
): boolean {
  const query = filterText.trim().toLowerCase();
  if (!query) {
    return true;
  }
  const haystack = [
    item.pilotName,
    item.systemName || "",
    item.sources.join(" "),
    levelLabel(item.level),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function observationScore(item: PilotObservation): number {
  return typeof item.score === "number" ? item.score : 0;
}

function latestEventText(
  alerts: AlertItem[],
  observations: PilotObservation[],
): string {
  const alert = alerts[0];
  if (alert) {
    return `在 ${alert.system_name || "未知星系"} 发现${
      (alert.names || []).join(", ") || "威胁目标"
    }`;
  }
  const observation = observations[0];
  if (observation) {
    return `${observation.pilotName} 出现在 ${observation.systemName || "未知星系"}`;
  }
  return "暂无最新威胁事件";
}

export function WorkbenchPage() {
  const [fitSignal, setFitSignal] = useState(0);
  const {
    filterText,
    selectedSystemId,
    setFilterText,
    setSelectedSystemId,
  } = useWorkbenchStore();
  const queryClient = useQueryClient();
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
  });

  const bootstrap = bootstrapQuery.data;
  const summary = bootstrap ? summarizeWorkbench(bootstrap) : null;
  const selected = selectedSystem(bootstrap, selectedSystemId);
  const generatedAt = bootstrap ? formatTime(bootstrap.generated_at) : "-";
  const observations = useMemo(() => {
    if (!bootstrap) {
      return [];
    }
    return buildPilotObservations(bootstrap, selectedSystemId).filter((item) =>
      matchesObservationFilter(item, filterText),
    );
  }, [bootstrap, filterText, selectedSystemId]);
  const graphData = useMemo(() => {
    if (!bootstrap) {
      return { links: [], nodes: [] };
    }
    return buildTacticalGraph(bootstrap, selectedSystemId);
  }, [bootstrap, selectedSystemId]);

  useEffect(() => {
    if (!bootstrapQuery.isSuccess) {
      return undefined;
    }
    const stream = connectAlerts((alert: AlertItem) => {
      queryClient.setQueryData<BootstrapPayload>(["bootstrap"], (current) => {
        if (!current) {
          return current;
        }
        const nextAlerts = [alert, ...current.alerts.filter((item) => item.id !== alert.id)];
        return { ...current, alerts: nextAlerts };
      });
    });
    return () => {
      stream.close();
    };
  }, [bootstrapQuery.isSuccess, queryClient]);

  const maxThreatScore = observations.reduce(
    (max, item) => Math.max(max, observationScore(item)),
    0,
  );
  const highRiskCount = observations.filter((item) =>
    item.level === "critical" || item.level === "high",
  ).length;
  const latestEvent = latestEventText(bootstrap?.alerts || [], observations);
  const activeSystemCount = bootstrap?.map.systems.filter((item) =>
    Number(item.hostile_count || 0) > 0 || Number(item.report_count || 0) > 0,
  ).length ?? 0;

  return (
    <main className="workbench-shell">
      <aside className="left-rail" aria-label="态势总览">
        <section className="brand-panel">
          <div>
            <p className="eyebrow">EVE 哨兵</p>
            <h1>预警情报工作台</h1>
          </div>
          <span>系统时间 {generatedAt}</span>
        </section>

        <section className="threat-status-panel">
          <div className="status-row">
            <span>安全等级状态</span>
            <strong>{highRiskCount > 0 ? "警戒" : "平稳"}</strong>
          </div>
          <div className="status-row">
            <span>当前威胁评分</span>
            <strong className="danger-text">{maxThreatScore}</strong>
          </div>
          <div className="status-row">
            <span>活跃星系</span>
            <strong>{activeSystemCount}</strong>
          </div>
        </section>

        <nav className="nav-panel" aria-label="工作台导航">
          {navItems.map(([label, Icon]) => (
            <button className={label === "仪表盘" ? "active" : ""} key={label} type="button">
              <Icon size={18} />
              <span>{label}</span>
            </button>
          ))}
        </nav>

        <section className="sector-panel">
          <div className="section-title section-title-row">
            <div>
              <Database size={16} />
              <span>区域概览</span>
            </div>
            <ChevronDown size={15} />
          </div>
          <div className="sector-preview" aria-hidden="true" />
          <div className="sector-stat">
            <span>敌对活动</span>
            <strong className="danger-text">{summary?.alerts ?? 0}</strong>
          </div>
          <div className="sector-stat">
            <span>跃迁通道</span>
            <strong>{bootstrap?.map.links.length ?? 0}</strong>
          </div>
          <div className="sector-stat">
            <span>本地信号</span>
            <strong>{summary?.reports ?? 0}</strong>
          </div>
        </section>
      </aside>

      <section className="center-stack" aria-label="星图工作区">
        <section className="map-pane">
          <header className="map-toolbar">
            <label className="select-control">
              <span>区域：</span>
              <strong>Tenal</strong>
              <ChevronDown size={15} />
            </label>
            <label className="select-control">
              <span>视图模式：</span>
              <strong>安全态势</strong>
              <ChevronDown size={15} />
            </label>
            <label className="select-control">
              <span>视图：</span>
              <strong>星图</strong>
              <ChevronDown size={15} />
            </label>
            <label className="search-control">
              <Filter size={15} />
              <input
                aria-label="情报过滤"
                value={filterText}
                onChange={(event) => setFilterText(event.target.value)}
                placeholder="过滤：敌对活动"
              />
            </label>
            <button className="icon-button" type="button" aria-label="列表视图">
              <ClipboardList size={17} />
            </button>
            <button className="icon-button" type="button" aria-label="设置">
              <Settings size={17} />
            </button>
          </header>

          <div className="map-canvas">
            <div className="map-legend">
              <strong>图例</strong>
              <span><i className="legend-dot safe" />高安全区</span>
              <span><i className="legend-dot watch" />低安全区</span>
              <span><i className="legend-dot danger" />敌对活动</span>
              <span><i className="legend-dot monitor" />监控节点</span>
              <span><i className="legend-line" />跃迁通道</span>
              <span><i className="legend-line dashed" />跃迁抑制</span>
            </div>
            <TacticalStarMap
              fitSignal={fitSignal}
              graphData={graphData}
              onSelectSystem={setSelectedSystemId}
            />
            <div className="map-tools" aria-label="星图工具">
              <span>滚轮缩放</span>
              <span>拖拽平移</span>
              <span>{selected?.name ? `锁定 ${selected.name}` : "未锁定星系"}</span>
              <button
                type="button"
                aria-label="Fit 星图"
                onClick={() => setFitSignal((value) => value + 1)}
              >
                Fit
              </button>
              <button type="button">2D</button>
            </div>
          </div>
        </section>

        <section className="lower-grid">
          <article className="panel fleet-panel">
            <div className="section-title">
              <ShipWheel size={16} />
              <span>情报动向</span>
            </div>
            <div className="intel-table compact">
              {observations.slice(0, 5).map((item) => (
                <div className="intel-row" key={item.id}>
                  <span>{formatClock(item.latestSeen)}</span>
                  <strong>{item.systemName || "未知星系"}</strong>
                  <span>{item.pilotName}</span>
                  <em>{levelLabel(item.level)}</em>
                </div>
              ))}
              {observations.length === 0 ? <div className="table-empty">暂无观察记录</div> : null}
            </div>
          </article>
          <article className="panel score-panel">
            <div className="section-title">
              <Gauge size={16} />
              <span>威胁评分</span>
            </div>
            <ThreatGauge
              alerts={summary?.alerts ?? 0}
              hostiles={summary?.hostiles ?? 0}
              observations={observations.length}
              score={maxThreatScore}
              title="评分"
            />
          </article>
        </section>
      </section>

      <aside className="right-rail" aria-label="情报详情">
        <section className="panel observation-panel">
          <div className="section-title section-title-row">
            <div>
              <Skull size={16} />
              <span>敌对飞行员观察列表</span>
            </div>
            <strong>{observations.length}</strong>
          </div>
          <ObservationTable observations={observations} />
        </section>

        <section className="panel risk-panel">
          <div className="section-title">
            <Gauge size={16} />
            <span>ISK 损失风险</span>
          </div>
          <ThreatGauge
            alerts={summary?.alerts ?? 0}
            hostiles={summary?.hostiles ?? 0}
            observations={observations.length}
            score={maxThreatScore}
            title="风险"
          />
        </section>

        <section className="panel alert-panel">
          <div className="section-title section-title-row">
            <div>
              <AlertTriangle size={16} />
              <span>告警队列</span>
            </div>
            <strong className="danger-text">{bootstrap?.alerts.length ?? 0}</strong>
          </div>
          <div className="intel-table">
            {(bootstrap?.alerts || []).slice(0, 6).map((item) => (
              <div className="intel-row alert-row" key={item.id}>
                <span>{formatClock(item.created_at)}</span>
                <strong>{item.system_name || "未知星系"}</strong>
                <em>{levelLabel(item.level)}</em>
                <span>{item.acknowledged ? "确认中" : "新"}</span>
              </div>
            ))}
            {(!bootstrap || bootstrap.alerts.length === 0) ? (
              <div className="table-empty">暂无告警</div>
            ) : null}
          </div>
        </section>
      </aside>

      <footer className="bottom-bar" aria-label="最新事件">
        <div className="quick-icons">
          <button type="button"><Radio size={16} /></button>
          <button type="button"><Bell size={16} /></button>
          <button type="button"><Database size={16} /></button>
        </div>
        <span>数据状态：<strong className="online-dot">在线</strong></span>
        <div className="latest-event">
          <AlertTriangle size={17} />
          <strong>最新事件</strong>
          <time>{formatClock(bootstrap?.generated_at)}</time>
          <span>{latestEvent}</span>
        </div>
        <span>成员：{summary?.onlineClients ?? 0}</span>
        <span>星系：{summary?.systems ?? 0}</span>
      </footer>
    </main>
  );
}
