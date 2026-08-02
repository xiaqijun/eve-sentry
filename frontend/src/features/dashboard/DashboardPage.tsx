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
  label: string;
  clientType: string;
  systemName: string;
  online: boolean;
  stale: boolean;
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
    .map((heartbeat, index) => {
      const details = typeof heartbeat.details === "object" && heartbeat.details !== null
        ? heartbeat.details as Record<string, unknown>
        : {};
      const id = String(heartbeat.client_id || `client-${index}`);
      const online = heartbeat.online === true;
      const status = String(heartbeat.status || details.status || "").toLowerCase();
      return {
        id,
        label: String(heartbeat.label || details.label || id),
        clientType: String(heartbeat.client_type || "unknown"),
        systemName: String(
          heartbeat.system_name || details.system_name || details.system || "-",
        ),
        online,
        stale: heartbeat.stale === true || status === "stale" || status === "error",
        lastSeen: String(
          heartbeat.last_seen_at || heartbeat.received_at || details.last_success_at || "",
        ) || undefined,
      };
    })
    .sort((left, right) => (
      Number(left.online && !left.stale) - Number(right.online && !right.stale)
      || left.label.localeCompare(right.label)
    ));
}

function dangerTag(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <Typography.Text type="secondary">暂无数据</Typography.Text>;
  }
  const color = value >= 80 ? "red" : value >= 60 ? "orangered" : value >= 40 ? "orange" : "green";
  return <Tag color={color}>zKill {Math.round(value)}</Tag>;
}

export function DashboardPage() {
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
    refetchInterval: REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
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
  const pendingAlerts = (bootstrapQuery.data ? currentRedAlerts(bootstrapQuery.data) : [])
    .filter((alert) => !alert.acknowledged)
    .sort((left, right) => (
      new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime()
    ));
  const currentHostiles = systemNodes.reduce((sum, node) => sum + node.hostileCount, 0);
  const onlineSystems = systemNodes.filter((node) => node.monitorOnlineCount > 0).length;
  const abnormalClients = clients.filter((client) => !client.online || client.stale).length;

  const systemColumns: TableColumnProps<LiveSystemRow>[] = [
    { title: "星系", dataIndex: "name", width: 130, render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "当前敌对", dataIndex: "hostileCount", width: 92 },
    { title: "已验证人员", dataIndex: "names", render: (value: string[]) => value.length > 0 ? value.slice(0, 4).join("、") : "等待身份补全" },
    { title: "最高危险度", dataIndex: "maxDangerRatio", width: 118, render: (value: number | null) => dangerTag(value) },
    { title: "在线节点", dataIndex: "monitorOnlineCount", width: 92 },
    { title: "最后变化", dataIndex: "lastSeen", width: 168, render: (value?: string) => formatTime(value) },
  ];
  const alertColumns: TableColumnProps<AlertItem>[] = [
    { title: "时间", dataIndex: "created_at", width: 168, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 120, render: (value?: string) => value || "未知星系" },
    { title: "已验证人员", dataIndex: "verified_characters", render: (value?: VerifiedCharacter[]) => (value || []).map((item) => item.name).join("、") || "等待身份补全" },
    { title: "处置", dataIndex: "acknowledged", width: 90, render: () => <Tag color="red">待确认</Tag> },
  ];
  const clientColumns: TableColumnProps<ClientStatusRow>[] = [
    { title: "客户端", dataIndex: "label", render: (value: string) => <Typography.Text bold ellipsis={{ showTooltip: true }}>{value}</Typography.Text> },
    { title: "类型", dataIndex: "clientType", width: 112 },
    { title: "星系", dataIndex: "systemName", width: 112 },
    { title: "状态", dataIndex: "online", width: 90, render: (_: boolean, row) => row.online && !row.stale ? <Tag color="green">在线</Tag> : <Tag color="red">异常</Tag> },
    { title: "最后上报", dataIndex: "lastSeen", width: 168, render: (value?: string) => formatTime(value) },
  ];

  return (
    <div className="dashboard-page">
      <header className="arco-page-header dashboard-header">
        <div>
          <Typography.Text className="content-page-kicker">实时处置</Typography.Text>
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
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<BellRing size={17} />} title="待确认告警" value={pendingAlerts.length} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<WifiOff size={17} />} title="异常客户端" value={abnormalClients} /></Card></Grid.Col>
      </Grid.Row>

      <Card className="dashboard-card dashboard-live-card" title={<span><Radar size={16} />当前敌对星系</span>} extra={<Tag color="red">实时</Tag>}>
        {systems.length > 0 ? (
          <Table<LiveSystemRow> border={false} columns={systemColumns} data={systems} pagination={false} rowKey="id" scroll={{ x: 820 }} />
        ) : <Empty description="当前监控范围内没有已确认敌对" />}
      </Card>

      <Grid.Row className="dashboard-secondary-grid" gutter={16}>
        <Grid.Col lg={14} xs={24}>
          <Card className="dashboard-card" title={<span><ShieldAlert size={16} />待处置告警</span>} extra={<Typography.Text type="secondary">仅显示未确认敌对</Typography.Text>}>
            {pendingAlerts.length > 0 ? (
              <Table<AlertItem> border={false} columns={alertColumns} data={pendingAlerts.slice(0, 8)} pagination={false} rowKey="id" scroll={{ x: 640 }} />
            ) : <Empty description="没有待确认告警" />}
          </Card>
        </Grid.Col>
        <Grid.Col lg={10} xs={24}>
          <Card className="dashboard-card" title={<span><MonitorCheck size={16} />监控覆盖</span>} extra={<Typography.Text type="secondary">{clients.length} 个客户端</Typography.Text>}>
            {clients.length > 0 ? (
              <Table<ClientStatusRow> border={false} columns={clientColumns} data={clients.slice(0, 8)} pagination={false} rowKey="id" scroll={{ x: 600 }} />
            ) : <Empty description="暂无客户端心跳" />}
          </Card>
        </Grid.Col>
      </Grid.Row>
    </div>
  );
}
