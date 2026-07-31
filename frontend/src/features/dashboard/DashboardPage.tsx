import { useMemo } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Grid,
  Progress,
  Statistic,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { IconRefresh } from "@arco-design/web-react/icon";
import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import {
  Activity,
  BellRing,
  MapPinned,
  MonitorCheck,
  ShieldAlert,
  Skull,
} from "lucide-react";

import { EveChart } from "../../components/EveChart";
import { type ThemeMode, useTheme } from "../shell/ThemeContext";
import { fetchHostileAlertHistory } from "../reports/api";
import { buildHostileReport } from "../reports/reporting";
import { fetchBootstrap } from "../workbench/api";
import { buildTacticalGraph, type TacticalGraphNode } from "../workbench/tacticalGraph";
import type { AlertItem, Level } from "../workbench/types";

const REFRESH_INTERVAL_MS = 60000;

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

function levelColor(level?: Level | "unknown"): string {
  if (level === "critical") return "red";
  if (level === "high") return "orangered";
  if (level === "medium") return "orange";
  if (level === "low") return "green";
  return "gray";
}

function trendOption(labels: string[], values: number[], theme: ThemeMode): EChartsOption {
  const dark = theme === "dark";
  return {
    animationDuration: 350,
    grid: { top: 18, right: 14, bottom: 28, left: 36 },
    tooltip: dark ? { trigger: "axis", backgroundColor: "#172328", borderColor: "#34474d", textStyle: { color: "#e5efec" } } : { trigger: "axis" },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: labels,
      axisLine: { lineStyle: { color: dark ? "#3b4c52" : "#dfe5e2" } },
      axisLabel: { color: dark ? "#91a3a2" : "#7c8882", fontSize: 11 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: dark ? "#829593" : "#8a9490", fontSize: 11 },
      splitLine: { lineStyle: { color: dark ? "#25343a" : "#edf0ef" } },
    },
    series: [{
      type: "line",
      data: values,
      smooth: 0.25,
      symbol: "circle",
      symbolSize: 7,
      lineStyle: { color: "#176b50", width: 2.5 },
      itemStyle: { color: dark ? "#4bb486" : "#176b50", borderColor: dark ? "#111b20" : "#ffffff", borderWidth: 2 },
      areaStyle: { color: "rgba(23,107,80,0.10)" },
    }],
  };
}

function severityOption(rows: Array<{ level: Level | "unknown"; count: number }>, theme: ThemeMode): EChartsOption {
  const dark = theme === "dark";
  const colors: Record<Level | "unknown", string> = {
    critical: "#c9362b",
    high: "#e36b32",
    medium: "#d7a02a",
    low: "#3b8f6c",
    unknown: "#9aa4a0",
  };
  return {
    animationDuration: 350,
    tooltip: dark ? { trigger: "item", backgroundColor: "#172328", borderColor: "#34474d", textStyle: { color: "#e5efec" } } : { trigger: "item" },
    legend: {
      bottom: 0,
      icon: "circle",
      itemHeight: 8,
      itemWidth: 8,
      textStyle: { color: dark ? "#91a3a2" : "#68746e", fontSize: 11 },
    },
    series: [{
      type: "pie",
      radius: ["50%", "72%"],
      center: ["50%", "43%"],
      label: { show: false },
      data: rows.filter((row) => row.count > 0).map((row) => ({
        name: levelLabel(row.level),
        value: row.count,
        itemStyle: { color: colors[row.level] },
      })),
    }],
  };
}

export function DashboardPage() {
  const { theme } = useTheme();
  const bootstrapQuery = useQuery({
    queryKey: ["bootstrap"],
    queryFn: fetchBootstrap,
    refetchInterval: REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
  });
  const historyQuery = useQuery({
    queryKey: ["hostile-alert-history", "7d"],
    queryFn: () => fetchHostileAlertHistory("7d"),
    refetchInterval: REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
  });

  const graphData = useMemo(
    () => bootstrapQuery.data ? buildTacticalGraph(bootstrapQuery.data, null) : { links: [], nodes: [] },
    [bootstrapQuery.data],
  );
  const report = useMemo(
    () => buildHostileReport(historyQuery.data?.alerts || [], "7d"),
    [historyQuery.data?.alerts],
  );
  const onlineSystems = graphData.nodes.filter((node) => node.monitorOnlineCount > 0).length;
  const currentHostiles = graphData.nodes.reduce((sum, node) => sum + node.hostileCount, 0);
  const pendingAlerts = (bootstrapQuery.data?.alerts || []).filter((alert) => !alert.acknowledged).length;
  const hotspotNodes = graphData.nodes
    .filter((node) => node.hostileCount > 0 || (node.killCount ?? 0) > 0 || node.monitorOnlineCount > 0)
    .sort((left, right) => (
      right.hostileCount - left.hostileCount
      || (right.killCount || 0) - (left.killCount || 0)
      || right.monitorOnlineCount - left.monitorOnlineCount
    ))
    .slice(0, 6);

  const refreshing = bootstrapQuery.isFetching || historyQuery.isFetching;
  const refresh = () => {
    void bootstrapQuery.refetch();
    void historyQuery.refetch();
  };

  const hotspotColumns = [
    { title: "星系", dataIndex: "name", render: (_: unknown, node: TacticalGraphNode) => <Typography.Text bold>{node.name}</Typography.Text> },
    { title: "当前敌对", dataIndex: "hostileCount", width: 100 },
    { title: "在线节点", dataIndex: "monitorOnlineCount", width: 100 },
    { title: "舰船损失", dataIndex: "killCount", width: 100 },
  ];
  const recentColumns = [
    { title: "时间", dataIndex: "created_at", width: 168, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 120, render: (value?: string) => value || "未知星系" },
    { title: "已验证目标", dataIndex: "names", render: (_: unknown, alert: AlertItem) => (alert.names || []).join("、") || "-" },
    { title: "风险", dataIndex: "level", width: 92, render: (value?: Level) => <Tag color={levelColor(value)}>{levelLabel(value)}</Tag> },
    { title: "状态", dataIndex: "acknowledged", width: 92, render: (value?: boolean) => value ? "已确认" : "待确认" },
  ];

  return (
    <div className="dashboard-page">
      <header className="arco-page-header dashboard-header">
        <div>
          <Typography.Text className="content-page-kicker">实时作战概览</Typography.Text>
          <Typography.Title heading={4}>仪表盘</Typography.Title>
        </div>
        <Button icon={<IconRefresh />} loading={refreshing} type="outline" onClick={refresh}>刷新数据</Button>
      </header>

      {bootstrapQuery.isError || historyQuery.isError ? (
        <Alert type="error" content="部分态势数据加载失败，请刷新后重试。" />
      ) : null}

      <Grid.Row className="arco-summary-grid dashboard-kpis" gutter={16}>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<MonitorCheck size={17} />} title="在线监控星系" value={onlineSystems} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<Skull size={17} />} title="当前敌对人数" value={currentHostiles} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<BellRing size={17} />} title="待确认告警" value={pendingAlerts} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<ShieldAlert size={17} />} title="7 天有效来袭" value={report.incidentCount} /></Card></Grid.Col>
      </Grid.Row>

      <Grid.Row className="dashboard-primary-grid" gutter={16}>
        <Grid.Col lg={16} xs={24}>
          <Card className="dashboard-card" title={<span><Activity size={16} />有效来袭趋势</span>} extra={<Tag color="green">7 天</Tag>}>
            {report.incidentCount > 0 ? (
              <EveChart height={270} option={trendOption(report.trend.map((item) => item.label), report.trend.map((item) => item.count), theme)} />
            ) : <Empty description="近 7 天暂无已验证敌对来袭" />}
          </Card>
        </Grid.Col>
        <Grid.Col lg={8} xs={24}>
          <Card className="dashboard-card dashboard-quality-card" title={<span><ShieldAlert size={16} />风险与数据质量</span>}>
            {report.incidentCount > 0 ? <EveChart height={200} option={severityOption(report.severity, theme)} /> : <Empty description="暂无风险分布" />}
            <div className="dashboard-quality-row"><span>有效数据率</span><strong>{report.verificationRate.toFixed(0)}%</strong></div>
            <Progress percent={report.verificationRate} showText={false} color="#176b50" />
            <div className="dashboard-quality-meta">
              <span>原始 {report.sourceCount}</span>
              <span>排除 {report.excludedCount}</span>
              <span>高危 {report.highRiskCount}</span>
            </div>
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Grid.Row className="dashboard-secondary-grid" gutter={16}>
        <Grid.Col lg={12} xs={24}>
          <Card className="dashboard-card" title={<span><MapPinned size={16} />当前热点星系</span>}>
            <Table<TacticalGraphNode> border={false} columns={hotspotColumns} data={hotspotNodes} pagination={false} rowKey="id" />
          </Card>
        </Grid.Col>
        <Grid.Col lg={12} xs={24}>
          <Card className="dashboard-card dashboard-brief-card" title={<span><Activity size={16} />7 天情报摘要</span>}>
            <div className="dashboard-brief-grid">
              <div><span>独立敌对</span><strong>{report.uniqueTargets}</strong><small>个已验证角色</small></div>
              <div><span>涉及星系</span><strong>{report.systemCount}</strong><small>个有效星系</small></div>
              <div><span>重复出现</span><strong>{report.repeatTargetCount}</strong><small>个高频目标</small></div>
              <div><span>跨星系活动</span><strong>{report.crossSystemTargetCount}</strong><small>个流动目标</small></div>
            </div>
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Card className="dashboard-card dashboard-recent-card" title={<span><BellRing size={16} />最近有效来袭</span>} extra={<Typography.Text type="secondary">更新于 {formatTime(historyQuery.data?.generatedAt)}</Typography.Text>}>
        <Table<AlertItem> border={false} columns={recentColumns} data={report.recent.slice(0, 6)} pagination={false} rowKey="id" />
      </Card>
    </div>
  );
}
