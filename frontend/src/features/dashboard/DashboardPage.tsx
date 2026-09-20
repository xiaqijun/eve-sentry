import { useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Grid,
  Spin,
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
} from "../workbench/types";
import { deriveClientHealth } from "../clients/clientHealth";
import { intelFeedback } from "../workbench/intelFeedback";
import { PilotLink, SnapshotTime, SystemLink } from "./QuickIntelLinks";

const REFRESH_INTERVAL_MS = 15000;

interface LiveSystemRow {
  id: string;
  name: string;
  hostileCount: number;
  monitorOnlineCount: number;
  names: string[];
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
        lastSeen: latestTime([
          ...matchingAlerts.map((alert) => alert.created_at),
          ...activeTimes,
        ]),
      };
    })
    .sort((left, right) => (
      right.hostileCount - left.hostileCount
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

export function DashboardPage() {
  const [detailId, setDetailId] = useState<string | null>(null);
  const detailTrigger = useRef<HTMLButtonElement | null>(null);
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
  const hasData = Boolean(bootstrapQuery.data);
  const selectedClient = clients.find((client) => client.id === detailId);
  const snapshotNow = bootstrapQuery.dataUpdatedAt || Date.now();
  const pilotIds = new Map(liveAlerts.flatMap((alert) => alert.verified_characters || [])
    .filter((pilot) => pilot.character_id > 0).map((pilot) => [pilot.name, pilot.character_id]));
  const refreshing = bootstrapQuery.isFetching;
  const updateLabel = bootstrapQuery.isError ? "更新失败"
    : refreshing ? "正在更新" : "每 15 秒更新";

  const systemColumns: TableColumnProps<LiveSystemRow>[] = [
    { title: "星系", dataIndex: "name", width: 110, render: (value: string) => <SystemLink name={value} /> },
    { title: "当前敌对", dataIndex: "hostileCount", width: 78 },
    { title: "人员 · zKill", dataIndex: "names", render: (value: string[]) => value.length > 0 ? <span className="table-token-list">{value.map((name, index) => <PilotLink key={`${name}:${index}`} name={name} id={pilotIds.get(name)} />)}</span> : "等待身份补全" },
    { title: "在线节点", dataIndex: "monitorOnlineCount", width: 78 },
    { title: "最后变化", dataIndex: "lastSeen", width: 110, render: (value?: string) => <SnapshotTime value={value} now={snapshotNow} /> },
  ];
  const alertColumns: TableColumnProps<AlertItem>[] = [
    { title: "时间", dataIndex: "created_at", width: 110, render: (value?: string) => <SnapshotTime value={value} now={snapshotNow} /> },
    { title: "星系", dataIndex: "system_name", width: 110, render: (value?: string) => <SystemLink name={value} /> },
    { title: "已验证人员 · zKill", dataIndex: "verified_characters", render: (value?: VerifiedCharacter[]) => (value || []).length > 0 ? <span className="table-token-list">{(value || []).map((item) => <PilotLink key={item.character_id} name={item.name} id={item.character_id} />)}</span> : "等待身份补全" },
  ];
  const clientColumns: TableColumnProps<ClientStatusRow>[] = [
    { title: "监控节点", dataIndex: "nodeLabel", width: 112, render: (value: string, row: ClientStatusRow) => <button className="console-data-link" onClick={(event) => { detailTrigger.current = event.currentTarget; setDetailId(row.id); }} aria-label={`查看${value}详情`}>{value}</button> },
    { title: "所在星系", dataIndex: "systemName", width: 96, render: (value: string) => <SystemLink name={value} /> },
    { title: "监控客户端", dataIndex: "clientLabel", render: (value: string) => <Typography.Text ellipsis={{ showTooltip: true }}>{value}</Typography.Text> },
    { title: "状态", dataIndex: "status", width: 92, render: (value: ClientStatusRow["status"]) => coverageStatusTag(value) },
    { title: "最后上报", dataIndex: "lastSeen", width: 110, render: (value?: string) => <SnapshotTime value={value} now={snapshotNow} /> },
  ];

  return (
    <div className="dashboard-page">
      <header className="arco-page-header dashboard-header">
        <div>
          <h1>工作台</h1>
          <Typography.Text type="secondary">先看当前敌情，再检查监控覆盖与异常。</Typography.Text>
        </div>
        <div className="dashboard-header-actions">
          <nav aria-label="工作台快捷入口" className="dashboard-shortcuts">
            <a href="/">打开星图</a>
            <a href="/reports/history">来袭历史</a>
          </nav>
          <Button icon={<IconRefresh />} loading={refreshing} type="outline" onClick={() => void bootstrapQuery.refetch()}>刷新实时数据</Button>
        </div>
      </header>

      <div className="dashboard-freshness" role="status">
        <Tag color={bootstrapQuery.isError ? "orange" : "gray"}>{updateLabel}</Tag>
        <span>{hasData ? `最近成功获取：${formatTime(new Date(bootstrapQuery.dataUpdatedAt).toISOString())}` : "尚未获取实时数据"}</span>
      </div>
      {bootstrapQuery.isError ? (
        <Alert type="error" content={hasData
          ? "刷新失败，以下为上次成功获取的数据，不代表最新态势。请刷新后重试。"
          : "实时态势数据加载失败，请刷新后重试。"} />
      ) : null}

      <Grid.Row className="arco-summary-grid dashboard-kpis" gutter={[16, 16]}>
        <Grid.Col lg={6} xs={12}><Card><Statistic prefix={<MonitorCheck size={17} />} title="在线监控星系" value={hasData ? onlineSystems : "—"} /></Card></Grid.Col>
        <Grid.Col lg={6} xs={12}><Card><Statistic prefix={<Skull size={17} />} title="当前敌对人数" value={hasData ? currentHostiles : "—"} /></Card></Grid.Col>
        <Grid.Col lg={6} xs={12}><Card><Statistic prefix={<BellRing size={17} />} title="当前告警事件" value={hasData ? liveAlerts.length : "—"} /></Card></Grid.Col>
        <Grid.Col lg={6} xs={12}><Card><Statistic prefix={<WifiOff size={17} />} title="异常客户端" value={hasData ? abnormalClients : "—"} /></Card></Grid.Col>
      </Grid.Row>

      {!hasData ? (
        <Card className="dashboard-card dashboard-initial-state" aria-busy={refreshing}>
          {refreshing ? <div role="status"><Spin /><p>正在获取监控与敌情数据…</p></div>
            : <Empty description="数据暂不可用，请点击上方刷新重试" />}
        </Card>
      ) : <>
      <Card className="dashboard-card dashboard-live-card" title={<span><Radar size={16} />当前敌对星系</span>} extra={<Tag color={bootstrapQuery.isError ? "orange" : systems.length ? "red" : "gray"}>{bootstrapQuery.isError ? "上次数据" : `${systems.length} 个星系`}</Tag>}>
        {systems.length > 0 ? (
          <Table<LiveSystemRow> border={false} columns={systemColumns} data={systems} pagination={false} rowKey="id" scroll={{ x: 600 }} />
        ) : <Empty description={intelFeedback(hasData, bootstrapQuery.isError, onlineSystems)} />}
      </Card>

      <Grid.Row className="dashboard-secondary-grid" gutter={16}>
        <Grid.Col xs={24} className="dashboard-alert-summary">
          <Card className="dashboard-card">
              <details className="dashboard-alert-details"><summary><ShieldAlert size={16} aria-hidden="true" />最新告警事件 · 展开当前告警明细（{liveAlerts.length} 条）</summary>
                <p>这里只展示当前快照中的告警，过往记录请查看<a className="console-data-link" href="/reports/history">来袭历史</a>。</p>
                {liveAlerts.length > 0 ? (
                <Table<AlertItem> border={false} columns={alertColumns} data={liveAlerts} pagination={liveAlerts.length > 8 ? { pageSize: 8, size: "mini" } : false} rowKey="id" scroll={{ x: 520 }} />
                ) : <Empty description="没有实时敌对告警" />}
              </details>
          </Card>
        </Grid.Col>
        <Grid.Col xs={24} className="dashboard-coverage">
          <Card className="dashboard-card" title={<span><MonitorCheck size={16} />监控覆盖</span>} extra={<Typography.Text type="secondary">{clients.length} 个节点 · {coveredSystems} 个星系</Typography.Text>}>
            {clients.length > 0 ? (
              <Table<ClientStatusRow> border={false} columns={clientColumns} data={clients} pagination={clients.length > 8 ? { pageSize: 8, size: "mini" } : false} rowKey="id" scroll={{ x: 620 }} />
            ) : <Empty description={<span>尚未收到监控节点覆盖信息<br />请先在客户端开启监控，再刷新查看。</span>} />}
          </Card>
        </Grid.Col>
      </Grid.Row>
      </>}
      <Drawer title="监控节点详情" visible={detailId !== null} onCancel={() => setDetailId(null)} afterClose={() => detailTrigger.current?.focus()} footer={null} width="min(460px, 94vw)" focusLock autoFocus escToExit unmountOnExit>
        <div role="dialog" aria-label="监控节点详情" aria-modal="true">
        {selectedClient ? <div className="console-node-detail">
          <p>{bootstrapQuery.isError ? "更新失败，以下为上次数据。" : "以下信息来自最近一次成功获取的节点上报。"}</p>
          <dl>
            <dt>节点</dt><dd>{selectedClient.nodeLabel}</dd>
            <dt>监控角色</dt><dd>{selectedClient.accountName}</dd>
            <dt>客户端</dt><dd>{selectedClient.clientLabel}</dd>
            <dt>所在星系</dt><dd><SystemLink name={selectedClient.systemName} /></dd>
            <dt>状态</dt><dd>{coverageStatusTag(selectedClient.status)}</dd>
            <dt>最后上报</dt><dd>{formatTime(selectedClient.lastSeen)}</dd>
          </dl>
        </div> : <Empty description="该节点已不在当前列表中，请关闭后刷新查看" />}
        </div>
      </Drawer>
    </div>
  );
}
