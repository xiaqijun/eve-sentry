import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  Database,
  Filter,
  LogIn,
  Skull,
} from "lucide-react";

import { connectAlerts, fetchBootstrap, startEsiLogin } from "./api";
import { buildPilotObservations } from "./observations";
import { ObservationTable } from "./ObservationTable";
import { useWorkbenchStore } from "./store";
import { buildTacticalGraph } from "./tacticalGraph";
import { TacticalStarMap } from "./TacticalStarMap";
import type {
  AlertItem,
  BootstrapPayload,
  Level,
  MapSystem,
  PilotObservation,
} from "./types";
import { summarizeWorkbench } from "./workbenchSummary";

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

function esiStatusText(bootstrap: BootstrapPayload | undefined): {
  publicState: string;
  authState: string;
  clientIdState: string;
  tokenState: string;
} {
  const esi = bootstrap?.esi;
  const config = esi?.config;
  return {
    publicState: esi?.enabled ? "已启用" : "未启用",
    authState: esi?.authenticated ? "已授权" : "未授权",
    clientIdState: config?.client_id_configured ? "已配置" : "未配置",
    tokenState: config?.token_file_present ? "已保存" : "未保存",
  };
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

interface LatestEventSummary {
  active: boolean;
  occurredAt?: string;
  text: string;
}

function latestEventSummary(
  alerts: AlertItem[],
  observations: PilotObservation[],
): LatestEventSummary {
  const alert = alerts[0];
  if (alert) {
    return {
      active: true,
      occurredAt: alert.created_at,
      text: `在 ${alert.system_name || "未知星系"} 发现${
        (alert.names || []).join(", ") || "威胁目标"
      }`,
    };
  }
  const observation = observations[0];
  if (observation) {
    return {
      active: true,
      occurredAt: observation.latestSeen,
      text: `${observation.pilotName} 出现在 ${observation.systemName || "未知星系"}`,
    };
  }
  return {
    active: false,
    text: "暂无实时威胁事件",
  };
}

export function WorkbenchPage() {
  const [fitSignal, setFitSignal] = useState(0);
  const [esiLoginStarting, setEsiLoginStarting] = useState(false);
  const [esiLoginError, setEsiLoginError] = useState("");
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
    const stream = connectAlerts(
      (alert: AlertItem) => {
        queryClient.setQueryData<BootstrapPayload>(["bootstrap"], (current) => {
          if (!current) {
            return current;
          }
          const nextAlerts = [alert, ...current.alerts.filter((item) => item.id !== alert.id)];
          return { ...current, alerts: nextAlerts };
        });
      },
      bootstrap?.generated_at,
    );
    return () => {
      stream.close();
    };
  }, [bootstrap?.generated_at, bootstrapQuery.isSuccess, queryClient]);

  const handleEsiLogin = useCallback(async () => {
    setEsiLoginStarting(true);
    setEsiLoginError("");
    const loginWindow = window.open("", "_blank");
    if (loginWindow) {
      loginWindow.opener = null;
    }
    try {
      const login = await startEsiLogin();
      const authorizationUrl = String(login.authorization_url || "").trim();
      if (!authorizationUrl) {
        loginWindow?.close();
        throw new Error(login.error || "服务端没有返回 ESI 授权地址");
      }
      if (loginWindow) {
        loginWindow.location.href = authorizationUrl;
      } else {
        window.open(authorizationUrl, "_blank", "noopener,noreferrer");
      }
      void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
    } catch (error) {
      loginWindow?.close();
      setEsiLoginError(
        error instanceof Error ? error.message : "ESI 登录启动失败",
      );
    } finally {
      setEsiLoginStarting(false);
    }
  }, [queryClient]);

  const highRiskCount = observations.filter((item) =>
    item.level === "critical" || item.level === "high",
  ).length;
  const latestEvent = latestEventSummary(bootstrap?.alerts || [], observations);
  const esiStatus = esiStatusText(bootstrap);
  const canStartEsiLogin = Boolean(bootstrap?.esi.config?.client_id_configured);
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
            <span>活跃星系</span>
            <strong>{activeSystemCount}</strong>
          </div>
        </section>

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

        <section className="panel esi-panel">
          <div className="section-title section-title-row">
            <div>
              <Database size={16} />
              <span>ESI 状态</span>
            </div>
            <strong className={bootstrap?.esi.authenticated ? "online-dot" : "danger-text"}>
              {esiStatus.authState}
            </strong>
          </div>
          <div className="intel-table">
            <div className="intel-row">
              <span>Public</span>
              <strong>{esiStatus.publicState}</strong>
              <em>Resolver</em>
              <span>{bootstrap?.esi.enabled ? "在线" : "离线"}</span>
            </div>
            <div className="intel-row">
              <span>SSO</span>
              <strong>{esiStatus.authState}</strong>
              <em>Client ID</em>
              <span>{esiStatus.clientIdState}</span>
            </div>
            <div className="intel-row">
              <span>Token</span>
              <strong>{esiStatus.tokenState}</strong>
              <em>Storage</em>
              <span>{bootstrap?.esi.config?.token_storage || "-"}</span>
            </div>
          </div>
          <div className="esi-actions">
            <button
              className="esi-login-button"
              disabled={!canStartEsiLogin || esiLoginStarting}
              type="button"
              onClick={handleEsiLogin}
            >
              <LogIn size={14} />
              {bootstrap?.esi.authenticated ? "重新登录" : "登录 ESI"}
            </button>
            {esiLoginError ? (
              <span className="esi-login-error" role="alert">
                {esiLoginError}
              </span>
            ) : null}
          </div>
        </section>
      </aside>

      <section className="center-stack" aria-label="星图工作区">
        <section className="map-pane">
          <header className="map-toolbar">
            <div className="toolbar-status">
              <span>区域：</span>
              <strong>Tenal</strong>
            </div>
            <div className="toolbar-status">
              <span>视图模式：</span>
              <strong>实时态势</strong>
            </div>
            <div className="toolbar-status">
              <span>视图：</span>
              <strong>星图</strong>
            </div>
            <label className="search-control">
              <Filter size={15} />
              <input
                aria-label="情报过滤"
                value={filterText}
                onChange={(event) => setFilterText(event.target.value)}
                placeholder="过滤：敌对活动"
              />
            </label>
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
              <span>2D</span>
            </div>
          </div>
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
        <span>数据状态：<strong className="online-dot">在线</strong></span>
        <div className={`latest-event ${latestEvent.active ? "" : "is-empty"}`}>
          <AlertTriangle size={17} />
          <strong>最新事件</strong>
          <time>{latestEvent.occurredAt ? formatClock(latestEvent.occurredAt) : "--:--"}</time>
          <span>{latestEvent.text}</span>
        </div>
        <span>成员：{summary?.onlineClients ?? 0}</span>
        <span>星系：{summary?.systems ?? 0}</span>
      </footer>
    </main>
  );
}
