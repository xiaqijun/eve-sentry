import { useCallback, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Activity,
  Bell,
  Database,
  Filter,
  LayoutDashboard,
  LogIn,
  Map,
  Radar,
  Skull,
} from "lucide-react";

import {
  connectAlerts,
  fetchBootstrap,
  fetchEsiLoginStatus,
  startEsiLogin,
} from "./api";
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

type WorkbenchNavId = "map" | "observations" | "alerts" | "esi";

const REALTIME_EVENT_WINDOW_MS = 60 * 60 * 1000;
const BOOTSTRAP_REFRESH_INTERVAL_MS = 60000;

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

function isRecentEvent(value: string | undefined, nowMs: number): boolean {
  if (!value) {
    return false;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return false;
  }
  const ageMs = nowMs - parsed.getTime();
  return ageMs >= 0 && ageMs <= REALTIME_EVENT_WINDOW_MS;
}

function latestEventSummary(
  alerts: AlertItem[],
  observations: PilotObservation[],
  nowMs: number = Date.now(),
): LatestEventSummary {
  const alert = alerts.find((item) => isRecentEvent(item.created_at, nowMs));
  if (alert) {
    return {
      active: true,
      occurredAt: alert.created_at,
      text: `在 ${alert.system_name || "未知星系"} 发现${
        (alert.names || []).join(", ") || "威胁目标"
      }`,
    };
  }
  const observation = observations.find((item) =>
    isRecentEvent(item.latestSeen, nowMs),
  );
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
  const [esiLoginPending, setEsiLoginPending] = useState(false);
  const [esiLoginStatus, setEsiLoginStatus] = useState("");
  const [esiLoginError, setEsiLoginError] = useState("");
  const [activeNav, setActiveNav] = useState<WorkbenchNavId>("map");
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
    refetchInterval: BOOTSTRAP_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
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
      undefined,
      (nextBootstrap: BootstrapPayload) => {
        queryClient.setQueryData<BootstrapPayload>(["bootstrap"], nextBootstrap);
      },
    );
    return () => {
      stream.close();
    };
  }, [bootstrapQuery.isSuccess, queryClient]);

  const handleEsiLogin = useCallback(async () => {
    setEsiLoginStarting(true);
    setEsiLoginPending(false);
    setEsiLoginStatus("");
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
      setEsiLoginStatus(login.status || "pending");
      setEsiLoginPending((login.status || "pending") === "pending");
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

  useEffect(() => {
    if (!esiLoginPending) {
      return undefined;
    }
    if (bootstrap?.esi.authenticated) {
      setEsiLoginPending(false);
      setEsiLoginStatus("authenticated");
      return undefined;
    }

    let cancelled = false;
    const refreshLoginStatus = async () => {
      try {
        const login = await fetchEsiLoginStatus();
        if (cancelled) {
          return;
        }
        const status = login.status || "idle";
        setEsiLoginStatus(status);
        void queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
        if (status === "authenticated") {
          setEsiLoginPending(false);
          return;
        }
        if (status === "error") {
          setEsiLoginPending(false);
          setEsiLoginError(login.error || "ESI 授权未完成");
        }
      } catch (error) {
        if (!cancelled) {
          setEsiLoginError(
            error instanceof Error ? error.message : "ESI 登录状态刷新失败",
          );
        }
      }
    };

    void refreshLoginStatus();
    const timer = window.setInterval(refreshLoginStatus, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [bootstrap?.esi.authenticated, esiLoginPending, queryClient]);

  const highRiskCount = observations.filter((item) =>
    item.level === "critical" || item.level === "high",
  ).length;
  const latestEvent = latestEventSummary(bootstrap?.alerts || [], observations);
  const esiStatus = esiStatusText(bootstrap);
  const canStartEsiLogin = Boolean(bootstrap?.esi.config?.client_id_configured);
  const activeSystemCount = graphData.nodes.filter((item) =>
    item.hostileCount > 0 || (item.killCount ?? 0) > 0 || item.monitorCount > 0,
  ).length;
  const navItems: {
    id: WorkbenchNavId;
    label: string;
    badge: string;
    status: string;
    panelId: string;
    icon: typeof Map;
  }[] = [
    {
      id: "map",
      label: "总览",
      badge: "全",
      status: "观察列表和告警队列同时显示",
      panelId: "workbench-detail-rail",
      icon: LayoutDashboard,
    },
    {
      id: "observations",
      label: "观察",
      badge: String(observations.length),
      status: "只显示敌对飞行员观察列表",
      panelId: "workbench-observation-panel",
      icon: Skull,
    },
    {
      id: "alerts",
      label: "告警",
      badge: String(bootstrap?.alerts.length ?? 0),
      status: "只显示实时告警队列",
      panelId: "workbench-alert-panel",
      icon: Bell,
    },
    {
      id: "esi",
      label: "ESI登录",
      badge: bootstrap?.esi.authenticated ? "已授权" : "未登录",
      status: "显示 ESI 登录、授权和连接状态",
      panelId: "workbench-esi-panel",
      icon: Database,
    },
  ];
  const activeNavItem = navItems.find((item) => item.id === activeNav) || navItems[0];
  const showObservationPanel = activeNav === "map" || activeNav === "observations";
  const showAlertPanel = activeNav === "map" || activeNav === "alerts";
  const showEsiPanel = activeNav === "esi";
  const rightRailClassName = `right-rail right-rail-${activeNav}`;
  const activateNav = (id: WorkbenchNavId) => {
    setActiveNav(id);
    if (window.innerWidth <= 900) {
      const targetId = id === "map" ? "workbench-map-panel" : "workbench-detail-rail";
      document.getElementById(targetId)?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  };
  const esiPanel = (
    <section className="panel esi-panel" id="workbench-esi-panel">
      <div className="section-title section-title-row">
        <div>
          <Database size={16} />
          <span>ESI 状态</span>
        </div>
        <strong className={bootstrap?.esi.authenticated ? "online-dot" : "danger-text"}>
          {esiStatus.authState}
        </strong>
      </div>
      <div className="esi-summary-grid">
        <div className="esi-summary-card">
          <span>Public Resolver</span>
          <strong className={bootstrap?.esi.enabled ? "online-dot" : "danger-text"}>
            {esiStatus.publicState}
          </strong>
        </div>
        <div className="esi-summary-card">
          <span>Client ID</span>
          <strong>{esiStatus.clientIdState}</strong>
        </div>
        <div className="esi-summary-card">
          <span>Token</span>
          <strong>{esiStatus.tokenState}</strong>
        </div>
        <div className="esi-summary-card">
          <span>存储</span>
          <strong>{bootstrap?.esi.config?.token_storage || "-"}</strong>
        </div>
      </div>
      <div className="esi-actions">
        <button
          className="esi-login-button"
          disabled={!canStartEsiLogin || esiLoginStarting || esiLoginPending}
          type="button"
          onClick={handleEsiLogin}
        >
          <LogIn size={14} />
          {esiLoginPending
            ? "等待授权"
            : bootstrap?.esi.authenticated
              ? "重新登录"
              : "登录 ESI"}
        </button>
        {esiLoginPending ? (
          <span className="esi-login-note">
            {esiLoginStatus === "pending" ? "等待 EVE 授权回调" : "正在检查授权状态"}
          </span>
        ) : null}
        {esiLoginError ? (
          <span className="esi-login-error" role="alert">
            {esiLoginError}
          </span>
        ) : null}
      </div>
    </section>
  );

  return (
    <main className="workbench-shell">
      <aside className="left-rail" aria-label="实时态势栏">
        <section className="brand-panel">
          <div>
            <p className="eyebrow">EVE 哨兵</p>
            <h1>预警情报工作台</h1>
          </div>
          <div className="rail-meta-grid">
            <div className="rail-meta">
              <span>状态更新时间</span>
              <strong>{generatedAt}</strong>
            </div>
            <div className="rail-meta">
              <span>在线客户端</span>
              <strong>{summary?.onlineClients ?? 0}</strong>
            </div>
          </div>
          <span className="rail-status-chip">
            <Activity size={13} />
            在线监控
          </span>
        </section>

        <nav className="nav-panel" aria-label="右侧面板切换">
          <div className="nav-panel-header">
            <span>右侧面板</span>
            <strong>{activeNavItem.label}</strong>
          </div>
          <div className="nav-panel-tabs" aria-label="右侧面板">
            {navItems.map((item) => {
              const Icon = item.icon;
              const selected = activeNav === item.id;
              return (
                <button
                  aria-controls={item.panelId}
                  aria-label={`切换到${item.label}面板`}
                  aria-pressed={selected}
                  className={selected ? "active" : ""}
                  data-nav-id={item.id}
                  key={item.id}
                  title={item.status}
                  type="button"
                  onClick={() => activateNav(item.id)}
                >
                  <Icon size={16} />
                  <span>
                    <strong>{item.label}</strong>
                  </span>
                  <b>{item.badge}</b>
                </button>
              );
            })}
          </div>
          <p className="nav-panel-status">
            <span>当前显示</span>
            <strong>{activeNavItem.status}</strong>
          </p>
        </nav>

        <section className="threat-status-panel">
          <div className="section-title compact-title">
            <Radar size={16} />
            <span>实时态势</span>
          </div>
          <div className="metric-grid">
            <div className="metric-card">
              <span>状态</span>
              <strong className={highRiskCount > 0 ? "danger-text" : ""}>
                {highRiskCount > 0 ? "警戒" : "平稳"}
              </strong>
            </div>
            <div className="metric-card">
              <span>活跃星系</span>
              <strong>{activeSystemCount}</strong>
            </div>
            <div className="metric-card">
              <span>敌对</span>
              <strong className="danger-text">{observations.length}</strong>
            </div>
            <div className="metric-card">
              <span>告警</span>
              <strong>{bootstrap?.alerts.length ?? 0}</strong>
            </div>
          </div>
        </section>

      </aside>

      <section className="center-stack" aria-label="星图工作区">
        <section className="map-pane" id="workbench-map-panel">
          <div className="map-canvas">
            <div className="map-legend">
              <strong>图例</strong>
              <span><i className="legend-dot monitor" />监控在线</span>
              <span><i className="legend-dot danger" />敌对</span>
            </div>
            <TacticalStarMap
              fitSignal={fitSignal}
              graphData={graphData}
              onSelectSystem={setSelectedSystemId}
            />
            <div className="map-tools" aria-label="星图工具">
              <span className="map-tool-status">
                {selected?.name ? `锁定 ${selected.name}` : "未锁定星系"}
              </span>
              <button
                type="button"
                aria-label="Fit 星图"
                onClick={() => setFitSignal((value) => value + 1)}
              >
                Fit
              </button>
              <span className="map-tool-mode">2D</span>
            </div>
          </div>
        </section>
      </section>

      <aside className={rightRailClassName} id="workbench-detail-rail" aria-label="情报详情">
        {showEsiPanel ? esiPanel : null}
        {showObservationPanel ? (
          <section className="panel observation-panel" id="workbench-observation-panel">
            <div className="section-title section-title-row">
              <div>
                <Skull size={16} />
                <span>敌对飞行员观察列表</span>
              </div>
              <strong>{observations.length}</strong>
            </div>
            <label className="observation-search">
              <Filter size={15} />
              <input
                aria-label="筛选敌对飞行员"
                type="search"
                value={filterText}
                onChange={(event) => setFilterText(event.target.value)}
                placeholder="搜索飞行员、星系或来源"
              />
            </label>
            <ObservationTable observations={observations} />
          </section>
        ) : null}

        {showAlertPanel ? (
          <section className="panel alert-panel" id="workbench-alert-panel">
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
        ) : null}
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
