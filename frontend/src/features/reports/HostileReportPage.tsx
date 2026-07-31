import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Grid,
  Progress,
  Radio,
  Statistic,
  Table,
  Tag,
  Typography,
  type TableColumnProps,
} from "@arco-design/web-react";
import { IconRefresh } from "@arco-design/web-react/icon";
import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  MapPinned,
  ShieldAlert,
  Skull,
  Users,
} from "lucide-react";

import { EveChart } from "../../components/EveChart";
import { type ThemeMode, useTheme } from "../shell/ThemeContext";
import { fetchHostileAlertHistory } from "./api";
import {
  buildHostileReport,
  type ReportRange,
  type SystemReportRow,
  type TargetReportRow,
} from "./reporting";
import type { AlertItem, Level } from "../workbench/types";

const REPORT_REFRESH_INTERVAL_MS = 60000;

const RANGE_OPTIONS: Array<{ value: ReportRange; label: string }> = [
  { value: "24h", label: "24 小时" },
  { value: "7d", label: "7 天" },
  { value: "30d", label: "30 天" },
  { value: "all", label: "全部" },
];

function formatTime(value?: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
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
    grid: { top: 18, right: 16, bottom: 28, left: 38 },
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
      showSymbol: values.length <= 12,
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
      radius: ["48%", "72%"],
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

export function HostileReportPage() {
  const { theme } = useTheme();
  const [range, setRange] = useState<ReportRange>("7d");
  const historyQuery = useQuery({
    queryKey: ["hostile-alert-history", range],
    queryFn: () => fetchHostileAlertHistory(range),
    refetchInterval: REPORT_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const report = useMemo(
    () => buildHostileReport(historyQuery.data?.alerts || [], range),
    [historyQuery.data?.alerts, range],
  );
  const activeRange = RANGE_OPTIONS.find((item) => item.value === range)?.label || "7 天";

  const systemColumns: TableColumnProps<SystemReportRow>[] = [
    { title: "星系", dataIndex: "name", render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "有效来袭", dataIndex: "incidentCount", width: 100, sorter: (a, b) => a.incidentCount - b.incidentCount },
    { title: "目标人次", dataIndex: "targetSightings", width: 100 },
    { title: "独立目标", dataIndex: "uniqueTargets", width: 100 },
    { title: "最后出现", dataIndex: "lastSeen", width: 168, render: (value?: string) => formatTime(value) },
  ];
  const targetColumns: TableColumnProps<TargetReportRow>[] = [
    { title: "已验证目标", dataIndex: "name", render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "出现批次", dataIndex: "incidentCount", width: 100, sorter: (a, b) => a.incidentCount - b.incidentCount },
    { title: "涉及星系", dataIndex: "systems", render: (value: string[]) => value.join("、") || "-" },
    { title: "最后出现", dataIndex: "lastSeen", width: 168, render: (value?: string) => formatTime(value) },
  ];
  const recentColumns: TableColumnProps<AlertItem>[] = [
    { title: "时间", dataIndex: "created_at", width: 168, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 120, render: (value?: string) => value || "未知星系" },
    { title: "已验证目标", dataIndex: "names", render: (value?: string[]) => (value || []).join("、") || "-" },
    { title: "风险", dataIndex: "level", width: 90, render: (value?: Level) => <Tag color={levelColor(value)}>{levelLabel(value)}</Tag> },
    { title: "状态", dataIndex: "acknowledged", width: 90, render: (value?: boolean) => value ? "已确认" : "待确认" },
  ];

  return (
    <div className="hostile-report-page">
      <header className="arco-page-header hostile-report-header">
        <div>
          <Typography.Text className="content-page-kicker">仅统计 ESI 已验证敌对角色</Typography.Text>
          <Typography.Title heading={4}>敌对来袭报表</Typography.Title>
        </div>
        <div className="hostile-report-actions">
          <Radio.Group aria-label="报表统计范围" type="button" value={range} onChange={(value) => setRange(value as ReportRange)}>
            {RANGE_OPTIONS.map((item) => <Radio key={item.value} value={item.value}>{item.label}</Radio>)}
          </Radio.Group>
          <Button icon={<IconRefresh />} loading={historyQuery.isFetching} type="outline" onClick={() => void historyQuery.refetch()}>刷新</Button>
        </div>
      </header>

      {historyQuery.isError ? (
        <Alert type="error" content={historyQuery.error instanceof Error ? historyQuery.error.message : "来袭历史加载失败"} />
      ) : null}

      <Grid.Row className="arco-summary-grid hostile-report-kpis" gutter={16}>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<CheckCircle2 size={17} />} title="有效来袭" value={report.incidentCount} extra={<Typography.Text type="secondary">{activeRange}内已验证事件</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<Users size={17} />} title="独立敌对" value={report.uniqueTargets} extra={<Typography.Text type="secondary">去重后的角色数量</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<ShieldAlert size={17} />} title="高危事件" value={report.highRiskCount} extra={<Typography.Text type="secondary">严重与高危事件</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<MapPinned size={17} />} title="涉及星系" value={report.systemCount} extra={<Typography.Text type="secondary">存在有效记录的星系</Typography.Text>} /></Card></Grid.Col>
      </Grid.Row>

      <Card className="report-data-quality" title={<span><CheckCircle2 size={16} />数据有效性</span>}>
        <div className="report-quality-main">
          <div><span>有效数据率</span><strong>{report.verificationRate.toFixed(0)}%</strong></div>
          <Progress percent={report.verificationRate} showText={false} color="#176b50" />
        </div>
        <div className="report-quality-stats">
          <div><span>原始记录</span><strong>{report.sourceCount}</strong></div>
          <div><span>有效记录</span><strong>{report.incidentCount}</strong></div>
          <div><span>排除噪声</span><strong>{report.excludedCount}</strong></div>
          <div><span>待确认</span><strong>{report.unacknowledgedCount}</strong></div>
          <div><span>目标人次</span><strong>{report.targetSightings}</strong></div>
          <div><span>平均每批</span><strong>{report.averageTargetsPerIncident.toFixed(1)}</strong></div>
        </div>
      </Card>

      <Grid.Row className="hostile-report-chart-grid" gutter={16}>
        <Grid.Col lg={16} xs={24}>
          <Card className="hostile-report-card" title={<span><BarChart3 size={16} />有效来袭趋势</span>} extra={<Tag color="green">{activeRange}</Tag>}>
            {report.incidentCount > 0 ? (
              <EveChart height={286} option={trendOption(report.trend.map((item) => item.label), report.trend.map((item) => item.count), theme)} />
            ) : <Empty description="当前范围暂无有效来袭趋势" />}
          </Card>
        </Grid.Col>
        <Grid.Col lg={8} xs={24}>
          <Card className="hostile-report-card" title={<span><ShieldAlert size={16} />风险分布</span>} extra={<Typography.Text type="secondary">高危 {report.highRiskRate.toFixed(0)}%</Typography.Text>}>
            {report.incidentCount > 0 ? <EveChart height={286} option={severityOption(report.severity, theme)} /> : <Empty description="暂无风险分布" />}
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Grid.Row className="hostile-report-ranking-grid" gutter={16}>
        <Grid.Col lg={12} xs={24}>
          <Card className="hostile-report-card" title={<span><MapPinned size={16} />星系来袭排行</span>} extra={<Typography.Text type="secondary">TOP 8</Typography.Text>}>
            <Table<SystemReportRow> border={false} columns={systemColumns} data={report.systems.slice(0, 8)} pagination={false} rowKey="name" scroll={{ x: 620 }} />
          </Card>
        </Grid.Col>
        <Grid.Col lg={12} xs={24}>
          <Card className="hostile-report-card" title={<span><Skull size={16} />高频敌对目标</span>} extra={<Typography.Text type="secondary">TOP 8</Typography.Text>}>
            <Table<TargetReportRow> border={false} columns={targetColumns} data={report.targets.slice(0, 8)} pagination={false} rowKey="characterId" scroll={{ x: 560 }} />
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Card className="hostile-report-card hostile-report-recent" title={<span><AlertTriangle size={16} />最近有效来袭</span>} extra={<Typography.Text type="secondary">更新于 {formatTime(historyQuery.data?.generatedAt)}</Typography.Text>}>
        <Table<AlertItem> border={false} columns={recentColumns} data={report.recent} pagination={false} rowKey="id" scroll={{ x: 760 }} />
      </Card>
    </div>
  );
}
