import { useEffect, useMemo, useState } from "react";
import { Button } from "@arco-design/web-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { connectAlerts, fetchBootstrap } from "./api";
import { useWorkbenchStore } from "./store";
import { buildTacticalGraph } from "./tacticalGraph";
import { TacticalStarMap } from "./TacticalStarMap";
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
      undefined,
      (nextBootstrap) => {
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
  return (
    <div className="star-map-workspace">
      <section className="star-map-stage" id="workbench-map-panel" aria-label="星图工作区">
        <TacticalStarMap
          fitSignal={fitSignal}
          graphData={graphData}
          onSelectSystem={setSelectedSystemId}
        />

        <section className="star-map-status" aria-label="态势统计">
          <div><span>在线预警节点</span><strong>{onlineMonitorNodeCount}</strong></div>
          <div><span>当前有敌星系</span><strong className={hostileSystemNodes.length > 0 ? "danger-text" : ""}>{hostileSystemNodes.length}</strong></div>
          <div><span>当前敌对人数</span><strong className={currentHostileCount > 0 ? "danger-text" : ""}>{currentHostileCount}</strong></div>
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
          <Button aria-label="Fit 星图" size="small" type="outline" onClick={() => setFitSignal((value) => value + 1)}>重置视图</Button>
        </div>

        {selected ? (
          <div className="star-map-selection">
            <span>已选星系</span>
            <strong>{selected.name}</strong>
            <small>{String(selected.region || "未知区域")} · ID {selected.system_id}</small>
          </div>
        ) : null}

        {bootstrapQuery.isError ? <div className="star-map-error" role="alert">星图态势加载失败</div> : null}
      </section>

    </div>
  );
}
