import { useMemo } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Grid,
  Statistic,
  Table,
  Tag,
  Typography,
  type TableColumnProps,
} from "@arco-design/web-react";
import { IconRefresh } from "@arco-design/web-react/icon";
import { useQuery } from "@tanstack/react-query";
import {
  BellRing,
  MonitorCheck,
  Radar,
  ShieldAlert,
  Skull,
  WifiOff,
} from "lucide-react";

import { fetchBootstrap } from "../workbench/api";
import {
  buildTacticalGraph,
  type TacticalGraphNode,
} from "../workbench/tacticalGraph";
import type {
  AlertItem,
  BootstrapPayload,
  VerifiedCharacter,
  ZkillStats,
} from "../workbench/types";
import { deriveClientHealth } from "../clients/clientHealth";

const REFRESH_INTERVAL_MS = 15000;

interface LiveSystemRow {
  id: string;
  name: string;
  hostileCount: number;
  monitorOnlineCount: number;
  names: string[];
  maxDangerRatio: number | null;
  lastSeen?: string;
}

interface ClientStatusRow {
  id: string;
  nodeLabel: string;
  accountName: string;
  clientLabel: string;
  systemName: string;
  status: "monitoring" | "warning" | "offline" | "incomplete";
  lastSeen?: string;
}

function formatTime(value?: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function latestTime(values: Array<string | undefined>): string | undefined {
  return values
    .filter((value): value is string => Boolean(value))
    .sort((left, right) => new Date(right).getTime() - new Date(left).getTime())[0];
}

function currentRedAlerts(bootstrap: BootstrapPayload): AlertItem[] {
  return bootstrap.alerts.filter((alert) => alert.classification === "red");
}

function zkillForCharacter(character: VerifiedCharacter): ZkillStats | undefined {
  return character.zkill && typeof character.zkill === "object"
    ? character.zkill
    : undefined;
}

function liveSystemRows(
  bootstrap: BootstrapPayload,
  systemNodes: TacticalGraphNode[],
): LiveSystemRow[] {
  const alerts = currentRedAlerts(bootstrap);
  return systemNodes
    .filter((node) => node.kind === "system" && node.hostileCount > 0)
    .map((node) => {
      const matchingAlerts = alerts.filter((alert) => (
        (node.systemId !== null && alert.system_id === node.systemId)
        || String(alert.system_name || "").trim() === node.name
      ));
      const characters = matchingAlerts.flatMap(
        (alert) => alert.verified_characters || [],
      );
      const names = [...new Set([
        ...characters.map((item) => item.name),
        ...matchingAlerts.flatMap((alert) => alert.names || []),
      ].map((name) => String(name).trim()).filter(Boolean))];
      const dangerRatios = characters
        .map((character) => zkillForCharacter(character)?.danger_ratio)
        .filter((value): value is number => typeof value === "number");
      const activeTimes = (bootstrap.active_intel || [])
        .filter((item) => (
          item.active !== false
          && (item.system_id === node.systemId || item.system_name === node.name)
        ))
        .map((item) => item.last_seen_at);
      return {
        id: node.id,
        name: node.name,
        hostileCount: node.hostileCount,
        monitorOnlineCount: node.monitorOnlineCount,
        names,
        maxDangerRatio: dangerRatios.length > 0 ? Math.max(...dangerRatios) : null,
        lastSeen: latestTime([
          ...matchingAlerts.map((alert) => alert.created_at),
          ...activeTimes,
        ]),
      };
    })
    .sort((left, right) => (
      right.hostileCount - left.hostileCount
      || (right.maxDangerRatio ?? -1) - (left.maxDangerRatio ?? -1)
      || left.name.localeCompare(right.name)
    ));
}

function clientStatusRows(bootstrap: BootstrapPayload): ClientStatusRow[] {
  return (bootstrap.clients?.heartbeats || [])
    .filter((heartbeat) => String(heartbeat.client_type || "") === "detector_client")
    .flatMap((heartbeat, heartbeatIndex) => {
      const details = typeof heartbeat.details === "object" && heartbeat.details !== null
        ? heartbeat.details as Record<string, unknown>
        : {};
      const id = String(heartbeat.client_id || `client-${heartbeatIndex}`);
      const online = heartbeat.online === true;
      const health = deriveClientHealth(heartbeat);
      const rawTargets = Array.isArray(details.targets) ? details.targets : [];
      const targets = rawTargets.length > 0
        ? rawTargets
        : details.monitoring !== false ? [{}] : [];
      const clientVersion = String(details.client_version || "").trim();
      const clientLabel = String(details.host || heartbeat.label || id).trim();
      const lastSeen = String(
        heartbeat.seen_at
        || heartbeat.last_seen_at
        || heartbeat.received_at
        || details.last_success_at
        || "",
      ) || undefined;
      return targets.flatMap((target, targetIndex) => {
        const value = target && typeof target === "object"
          ? target as Record<string, unknown>
          : {};
        if (value.monitoring === false) return [];
        const accountName = String(
          value.character_name || value.source_instance || value.window_title || "",
        ).trim();
        const systemName = String(
          value.system_name || heartbeat.system_name || details.system_name || details.system || "",
        ).trim();
        const runtimeStatus = String(value.runtime_status || "").trim().toLowerCase();
        const targetHasError = Boolean(String(value.last_error || "").trim())
          || ["error", "failed", "failure", "exception"].includes(runtimeStatus);
        return [{
          id: `${id}:${String(value.client_id || targetIndex)}`,
          nodeLabel: "",
          accountName: accountName || "未上报账号",
          clientLabel: `${clientLabel || id}${clientVersion ? ` · ${clientVersion}` : ""}`,
          systemName: systemName || "未知星系",
          status: !online
            ? "offline"
            : targetHasError || health.state === "warning"
              ? "warning"
              : accountName && systemName ? "monitoring" : "incomplete",
          lastSeen,
        } satisfies ClientStatusRow];
      });
    })
    .sort((left, right) => (
      Number(left.status === "monitoring") - Number(right.status === "monitoring")
      || left.accountName.localeCompare(right.accountName)
    ))
    .map((row, index) => ({ ...row, nodeLabel: `监控节点 ${index + 1}` }));
}

function coverageStatusTag(status: ClientStatusRow["status"]) {
  if (status === "monitoring") return <Tag color="green">监控中</Tag>;
  if (status === "warning") return <Tag color="red">运行异常</Tag>;
  if (status === "offline") return <Tag color="orange">客户端离线</Tag>;
  return <Tag color="gray">信息待补全</Tag>;
}

function dangerTag(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <Typography.Text type="secondary">暂无数据</Typography.Text>;
  }
  const color = value >= 80 ? "red" : value >= 60 ? "orangered" : value >= 40 ? "orange" : "green";
  return <Tag color={color}>威胁度 {Math.round(value)}</Tag>;
}

function levelTag(value?: string) {
  const normalized = String(value || "").toLowerCase();
  const labels: Record<string, string> = {
    critical: "紧急",
    high: "高",
    medium: "中",
    low: "低",
  };
  const colors: Record<string, string> = {
    critical: "red",
    high: "orangered",
    medium: "orange",
    low: "blue",
  };
  return <Tag color={colors[normalized] || "gray"}>{labels[normalized] || "未知"}</Tag>;
}

export function DashboardPage() {
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
    refetchInterval: REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  const graphData = useMemo(
    () => bootstrapQuery.data
      ? buildTacticalGraph(bootstrapQuery.data, null, { includeHostileCards: false })
      : { links: [], nodes: [] },
    [bootstrapQuery.data],
  );
  const systemNodes = graphData.nodes.filter((node) => node.kind === "system");
  const systems = useMemo(
    () => bootstrapQuery.data ? liveSystemRows(bootstrapQuery.data, systemNodes) : [],
    [bootstrapQuery.data, systemNodes],
  );
  const clients = useMemo(
    () => bootstrapQuery.data ? clientStatusRows(bootstrapQuery.data) : [],
    [bootstrapQuery.data],
  );
  const allHeartbeats = bootstrapQuery.data?.clients?.heartbeats || [];
  const liveAlerts = (bootstrapQuery.data ? currentRedAlerts(bootstrapQuery.data) : [])
    .sort((left, right) => (
      new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime()
    ));
  const currentHostiles = systemNodes.reduce((sum, node) => sum + node.hostileCount, 0);
  const onlineSystems = systemNodes.filter((node) => node.monitorOnlineCount > 0).length;
  const abnormalClients = allHeartbeats.filter(
    (client) => deriveClientHealth(client).isException,
  ).length;
  const coveredSystems = new Set(
    clients.map((client) => client.systemName).filter((name) => name !== "未知星系"),
  ).size;

  const systemColumns: TableColumnProps<LiveSystemRow>[] = [
    { title: "星系", dataIndex: "name", width: 110, render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "当前敌对", dataIndex: "hostileCount", width: 78 },
    { title: "已验证人员", dataIndex: "names", render: (value: string[]) => value.length > 0 ? <span className="table-token-list">{value.map((name, index) => <span key={`${name}:${index}`}>{name}</span>)}</span> : "等待身份补全" },
    { title: "最高威胁度", dataIndex: "maxDangerRatio", width: 108, render: (value: number | null) => dangerTag(value) },
    { title: "在线节点", dataIndex: "monitorOnlineCount", width: 78 },
    { title: "最后变化", dataIndex: "lastSeen", width: 154, render: (value?: string) => formatTime(value) },
  ];
  const alertColumns: TableColumnProps<AlertItem>[] = [
    { title: "时间", dataIndex: "created_at", width: 154, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 110, render: (value?: string) => value || "未知星系" },
    { title: "已验证人员", dataIndex: "verified_characters", render: (value?: VerifiedCharacter[]) => (value || []).length > 0 ? <span className="table-token-list">{(value || []).map((item) => <span key={item.character_id}>{item.name}</span>)}</span> : "等待身份补全" },
    { title: "级别", dataIndex: "level", width: 76, render: (value?: string) => levelTag(value) },
  ];
  const clientColumns: TableColumnProps<ClientStatusRow>[] = [
    { title: "监控节点", dataIndex: "nodeLabel", width: 112, render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "所在星系", dataIndex: "systemName", width: 96 },
    { title: "监控客户端", dataIndex: "clientLabel", render: (value: string) => <Typography.Text ellipsis={{ showTooltip: true }}>{value}</Typography.Text> },
    { title: "状态", dataIndex: "status", width: 92, render: (value: ClientStatusRow["status"]) => coverageStatusTag(value) },
    { title: "最后上报", dataIndex: "lastSeen", width: 154, render: (value?: string) => formatTime(value) },
  ];

  return (
    <div className="dashboard-page">
      <header className="arco-page-header dashboard-header">
        <div>
          <Typography.Text className="content-page-kicker">实时监控</Typography.Text>
          <Typography.Title heading={4}>工作台</Typography.Title>
        </div>
        <Button icon={<IconRefresh />} loading={bootstrapQuery.isFetching} type="outline" onClick={() => void bootstrapQuery.refetch()}>刷新实时数据</Button>
      </header>

      {bootstrapQuery.isError ? (
        <Alert type="error" content="实时态势数据加载失败，请刷新后重试。" />
      ) : null}

      <Grid.Row className="arco-summary-grid dashboard-kpis" gutter={16}>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<MonitorCheck size={17} />} title="在线监控星系" value={onlineSystems} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<Skull size={17} />} title="当前敌对人数" value={currentHostiles} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<BellRing size={17} />} title="当前告警事件" value={liveAlerts.length} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<WifiOff size={17} />} title="异常客户端" value={abnormalClients} /></Card></Grid.Col>
      </Grid.Row>

      <Card className="dashboard-card dashboard-live-card" title={<span><Radar size={16} />当前敌对星系</span>} extra={<Tag color="red">实时</Tag>}>
        {systems.length > 0 ? (
          <Table<LiveSystemRow> border={false} columns={systemColumns} data={systems} pagination={false} rowKey="id" />
        ) : <Empty description="当前监控范围内没有敌对" />}
      </Card>

      <Grid.Row className="dashboard-secondary-grid" gutter={16}>
        <Grid.Col xl={12} lg={24} xs={24}>
          <Card className="dashboard-card" title={<span><ShieldAlert size={16} />最新告警事件</span>} extra={<Typography.Text type="secondary">当前敌对</Typography.Text>}>
            {liveAlerts.length > 0 ? (
              <Table<AlertItem> border={false} columns={alertColumns} data={liveAlerts.slice(0, 8)} pagination={false} rowKey="id" />
            ) : <Empty description="没有实时敌对告警" />}
          </Card>
        </Grid.Col>
        <Grid.Col xl={12} lg={24} xs={24}>
          <Card className="dashboard-card" title={<span><MonitorCheck size={16} />监控覆盖</span>} extra={<Typography.Text type="secondary">{clients.length} 个节点 · {coveredSystems} 个星系</Typography.Text>}>
            {clients.length > 0 ? (
              <Table<ClientStatusRow> border={false} columns={clientColumns} data={clients} pagination={clients.length > 8 ? { pageSize: 8, size: "mini" } : false} rowKey="id" />
            ) : <Empty description="尚未收到监控节点覆盖信息" />}
          </Card>
        </Grid.Col>
      </Grid.Row>
    </div>
  );
}
