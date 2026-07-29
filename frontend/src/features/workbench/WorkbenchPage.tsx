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
  return bootstrap.map.systems.find((item) => item.system_id === selectedSystemId) || null;
}

interface VerifiedHostileSummary {
  hostileCount: number;
  systemCount: number;
}

function summarizeVerifiedHostiles(alerts: AlertItem[]): VerifiedHostileSummary {
  const characterIds = new Set<number>();
  const systems = new Set<string>();

  alerts.forEach((alert) => {
    if (alert.classification !== "red") {
      return;
    }
    const verifiedCharacterIds = (alert.verified_characters || [])
      .map((character) => Number(character?.character_id))
      .filter((characterId) => Number.isInteger(characterId) && characterId > 0);
    if (verifiedCharacterIds.length === 0) {
      return;
    }
    verifiedCharacterIds.forEach((characterId) => characterIds.add(characterId));

    const systemId = Number(alert.system_id);
    if (Number.isInteger(systemId) && systemId > 0) {
      systems.add(`id:${systemId}`);
      return;
    }
    const systemName = String(alert.system_name || "").trim().toLocaleLowerCase();
    if (systemName) {
      systems.add(`name:${systemName}`);
    }
  });

  return {
    hostileCount: characterIds.size,
    systemCount: systems.size,
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
    refetchIntervalInBackground: true,
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
      (nextBootstrap: BootstrapPayload) => {
        queryClient.setQueryData<BootstrapPayload>(["bootstrap"], nextBootstrap);
      },
    );
    return () => {
      stream.close();
    };
  }, [bootstrapQuery.isSuccess, queryClient]);

  const verifiedHostiles = summarizeVerifiedHostiles(bootstrap?.alerts || []);
  const onlineMonitorNodeCount = graphData.nodes.filter((item) =>
    item.monitorOnlineCount > 0,
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
          <div><span>当前有敌星系</span><strong className={verifiedHostiles.systemCount > 0 ? "danger-text" : ""}>{verifiedHostiles.systemCount}</strong></div>
          <div><span>当前敌对人数</span><strong className={verifiedHostiles.hostileCount > 0 ? "danger-text" : ""}>{verifiedHostiles.hostileCount}</strong></div>
          <div><span>更新时间</span><strong>{formatClock(bootstrap?.generated_at)}</strong></div>
        </section>

        <div className="star-map-legend map-legend">
          <strong>图层</strong>
          <span><i className="legend-dot monitor" />监控在线</span>
          <span><i className="legend-dot danger" />敌对活动</span>
          <span><i className="legend-dot intel" />情报记录</span>
          <span><i className="legend-dot loss" />舰船损失</span>
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
