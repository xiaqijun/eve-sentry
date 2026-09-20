import { useEffect, useMemo, useState } from "react";
import { Button } from "@arco-design/web-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { connectAlerts, fetchBootstrap } from "./api";
import { useWorkbenchStore } from "./store";
import { buildTacticalGraph } from "./tacticalGraph";
import { TacticalStarMap } from "./TacticalStarMap";
import { intelFeedback } from "./intelFeedback";
import type {
  AlertItem,
  BootstrapPayload,
  MapSnapshotPayload,
  MapSystem,
} from "./types";

const BOOTSTRAP_REFRESH_INTERVAL_MS = 60000;

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

function selectedSystem(
  bootstrap: BootstrapPayload | undefined,
  selectedSystemId: number | null,
): MapSystem | null {
  if (!bootstrap || typeof selectedSystemId !== "number") {
    return null;
  }
  const systems = Array.isArray(bootstrap.map?.systems) ? bootstrap.map.systems : [];
  return systems.find((item) => item.system_id === selectedSystemId) || null;
}

type BootstrapStreamUpdate = Partial<Omit<BootstrapPayload, "map">> & {
  map?: Partial<Omit<MapSnapshotPayload, "systems">> & {
    systems?: Array<Partial<MapSystem>>;
  };
};

function mapSystemKey(system: Partial<MapSystem>): string {
  const systemId = Number(system.system_id);
  if (Number.isInteger(systemId) && systemId > 0) {
    return `id:${systemId}`;
  }
  return `name:${String(system.name || system.system_name || "")
    .trim()
    .toLowerCase()}`;
}

function mapSystemNameKey(system: Partial<MapSystem>): string {
  return `name:${String(system.name || system.system_name || "")
    .trim()
    .toLowerCase()}`;
}

function mergeMapSystemState(
  current: MapSystem[],
  update: Array<Partial<MapSystem>>,
): MapSystem[] {
  if (update.length === 0) {
    return current.map((system) => ({ ...system, hostile_count: 0 }));
  }
  const updates = new Map<string, Partial<MapSystem>>();
  update.forEach((system) => {
    updates.set(mapSystemKey(system), system);
    updates.set(mapSystemNameKey(system), system);
  });
  const hasCompleteTopology = update.every(
    (system) =>
      typeof system.name === "string" &&
      typeof system.x === "number" &&
      typeof system.y === "number",
  );
  if (hasCompleteTopology && update.length >= current.length) {
    return update as MapSystem[];
  }
  return current.map((system) => {
    const next = updates.get(mapSystemKey(system)) || updates.get(mapSystemNameKey(system));
    return next
      ? { ...system, hostile_count: Number(next.hostile_count || 0) }
      : { ...system, hostile_count: 0 };
  });
}

export function mergeBootstrapStreamUpdate(
  current: BootstrapPayload,
  update: BootstrapStreamUpdate,
): BootstrapPayload {
  const mapUpdate = update.map;
  const compactSystems = Array.isArray(mapUpdate?.systems) &&
    (mapUpdate.systems.length === 0 ||
      !mapUpdate.systems.every(
        (system) =>
          typeof system.name === "string" &&
          typeof system.x === "number" &&
          typeof system.y === "number",
      ));
  return {
    ...current,
    ...update,
    map: {
      ...current.map,
      ...mapUpdate,
      systems: Array.isArray(mapUpdate?.systems)
        ? mergeMapSystemState(current.map.systems, mapUpdate.systems)
        : current.map.systems,
      links: Array.isArray(mapUpdate?.links)
        ? compactSystems
          ? current.map.links
          : mapUpdate.links
        : current.map.links,
      summary: {
        ...current.map.summary,
        ...(mapUpdate?.summary || {}),
      },
    },
    reports: Array.isArray(update.reports) ? update.reports : current.reports,
    observations: Array.isArray(update.observations)
      ? update.observations
      : current.observations,
    alerts: Array.isArray(update.alerts) ? update.alerts : current.alerts,
    active_intel: Array.isArray(update.active_intel)
      ? update.active_intel
      : current.active_intel,
  };
}

export function WorkbenchPage() {
  const [fitSignal, setFitSignal] = useState(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSystem = searchParams.get("system")?.trim() || "";
  const [streamFailed, setStreamFailed] = useState(false);
  const {
    selectedSystemId,
    setSelectedSystemId,
  } = useWorkbenchStore();
  const queryClient = useQueryClient();
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
    refetchInterval: BOOTSTRAP_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });

  const bootstrap = bootstrapQuery.data;
  const requestedId = bootstrap?.map.systems.find((system) =>
    system.name.toLowerCase() === requestedSystem.toLowerCase())?.system_id;
  useEffect(() => {
    if (requestedSystem && requestedId) setSelectedSystemId(requestedId);
  }, [requestedSystem, requestedId, setSelectedSystemId]);
  const selected = selectedSystem(bootstrap, selectedSystemId);
  const graphData = useMemo(() => {
    if (!bootstrap) {
      return { links: [], nodes: [] };
    }
    return buildTacticalGraph(bootstrap, selectedSystemId, {
      includeHostileCards: true,
    });
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
      () => setStreamFailed(true),
      (nextBootstrap) => {
        setStreamFailed(false);
        queryClient.setQueryData<BootstrapPayload>(["bootstrap"], (current) => (
          current
            ? mergeBootstrapStreamUpdate(current, nextBootstrap)
            : nextBootstrap as BootstrapPayload
        ));
      },
    );
    return () => {
      stream.close();
    };
  }, [bootstrapQuery.isSuccess, queryClient]);

  const hostileSystemNodes = graphData.nodes.filter(
    (item) => item.kind === "system" && item.hostileCount > 0,
  );
  const currentHostileCount = hostileSystemNodes.reduce(
    (sum, item) => sum + item.hostileCount,
    0,
  );
  const onlineMonitorNodeCount = graphData.nodes.filter((item) =>
    item.kind === "system" && item.monitorOnlineCount > 0,
  ).length;
  const dataFailed = bootstrapQuery.isError || streamFailed;
  const feedback = intelFeedback(Boolean(bootstrap), dataFailed, onlineMonitorNodeCount);
  const emptyContent = <div className="star-map-empty-feedback" role="status">
    <p>{feedback}</p>
    {bootstrap || dataFailed ? <div className="star-map-empty-actions">
      <a href="/dashboard">查看监控覆盖</a>
      <Button loading={bootstrapQuery.isFetching} onClick={() => void bootstrapQuery.refetch()}>刷新数据</Button>
    </div> : null}
  </div>;
  return (
    <div className="star-map-workspace">
      <section className="star-map-stage" id="workbench-map-panel" aria-label="星图工作区">
        <TacticalStarMap
          fitSignal={fitSignal}
          graphData={graphData}
          emptyContent={emptyContent}
          focusSystemId={requestedId}
          onSelectSystem={setSelectedSystemId}
        />

        <section className="star-map-status" aria-label="态势统计">
          <div><span>在线监控星系</span><strong>{bootstrap ? onlineMonitorNodeCount : "—"}</strong></div>
          <div><span>当前有敌星系</span><strong className={hostileSystemNodes.length > 0 ? "danger-text" : ""}>{bootstrap ? hostileSystemNodes.length : "—"}</strong></div>
          <div><span>当前敌对人数</span><strong className={currentHostileCount > 0 ? "danger-text" : ""}>{bootstrap ? currentHostileCount : "—"}</strong></div>
          <div><span>更新时间</span><strong>{formatClock(bootstrap?.generated_at)}</strong></div>
        </section>

        <div className="star-map-legend map-legend">
          <strong>节点状态</strong>
          <span><i className="legend-dot monitor" />在线监控</span>
          <span><i className="legend-dot danger" />实时敌对</span>
          <span><i className="legend-dot intel" />活跃情报</span>
          <span><i className="legend-dot loss" />近 1 小时损失</span>
          <span><i className="legend-dot selected" />当前选中</span>
          <small>数字徽标表示敌对人数或损失数</small>
        </div>

        <div className="star-map-tools" aria-label="星图工具">
          <div>
            <span>当前定位</span>
            <strong>{selected?.name || "全部星系"}</strong>
          </div>
          <Button aria-label="Fit 星图" size="small" type="outline" onClick={() => {
            const next = new URLSearchParams(searchParams);
            next.delete("system");
            setSearchParams(next, { replace: true });
            setSelectedSystemId(null);
            setFitSignal((value) => value + 1);
          }}>重置视图</Button>
        </div>

        {selected && !dataFailed && onlineMonitorNodeCount > 0 ? (
          <div className="star-map-selection">
            <span>已选星系</span>
            <strong>{selected.name}</strong>
            <small>{String(selected.region || "未知区域")} · ID {selected.system_id}</small>
          </div>
        ) : null}

        {(graphData.nodes.length > 0 && (dataFailed || onlineMonitorNodeCount === 0)) || (requestedSystem && bootstrap && !requestedId) ?
          <div className="star-map-feedback-banner" role={dataFailed ? "alert" : "status"}>
            {graphData.nodes.length > 0 && (dataFailed || onlineMonitorNodeCount === 0) ? <div>{feedback}</div> : null}
            {requestedSystem && bootstrap && !requestedId ? <div>当前星图没有 {requestedSystem}，未定位到该星系。</div> : null}
          </div> : null}
      </section>

    </div>
  );
}
