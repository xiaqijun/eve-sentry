import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Grid,
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
  Activity,
  Crosshair,
  MapPinned,
  RadioTower,
  ShieldAlert,
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
  type WaveReportRow,
} from "./reporting";

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

function formatCompactNumber(value?: number): string {
  if (value === undefined || !Number.isFinite(value)) return "-";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function dangerTag(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <Typography.Text type="secondary">暂无数据</Typography.Text>;
  }
  const color = value >= 80 ? "red" : value >= 60 ? "orangered" : value >= 40 ? "orange" : "green";
  const roundedValue = Math.round(value);
  return <Tag color={color} title={`威胁度 ${roundedValue}`}>{roundedValue}</Tag>;
}

function textTokens(values: string[], fallback: string = "-") {
  const clean = values.map((value) => String(value).trim()).filter(Boolean);
  if (clean.length === 0) return fallback;
  return (
    <span className="table-token-list">
      {clean.map((value, index) => <span key={`${value}:${index}`}>{value}</span>)}
    </span>
  );
}

function trendOption(labels: string[], values: number[], theme: ThemeMode): EChartsOption {
  const dark = theme === "dark";
  return {
    animationDuration: 350,
    grid: { top: 18, right: 18, bottom: 30, left: 40 },
    tooltip: dark
      ? { trigger: "axis", backgroundColor: "#172328", borderColor: "#34474d", textStyle: { color: "#e5efec" } }
      : { trigger: "axis" },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: labels,
      axisLine: { lineStyle: { color: dark ? "#3b4c52" : "#dfe5e2" } },
      axisLabel: { color: dark ? "#91a3a2" : "#68746e", fontSize: 11 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: dark ? "#829593" : "#68746e", fontSize: 11 },
      splitLine: { lineStyle: { color: dark ? "#25343a" : "#edf0ef" } },
    },
    series: [{
      type: "line",
      data: values,
      smooth: 0.22,
      showSymbol: values.length <= 12,
      symbolSize: 7,
      lineStyle: { color: "#176b50", width: 2.5 },
      itemStyle: { color: dark ? "#4bb486" : "#176b50" },
      areaStyle: { color: "rgba(23,107,80,0.10)" },
    }],
  };
}

export function HostileReportPage() {
  const { theme } = useTheme();
  const [range, setRange] = useState<ReportRange>("24h");
  const historyQuery = useQuery({
    queryKey: ["hostile-alert-history", range],
    queryFn: () => fetchHostileAlertHistory(range),
    refetchInterval: REPORT_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const report = useMemo(
    () => buildHostileReport(
      historyQuery.data?.alerts || [],
      range,
      Date.now(),
      historyQuery.data?.waves || [],
    ),
    [historyQuery.data?.alerts, historyQuery.data?.waves, range],
  );
  const activeRange = RANGE_OPTIONS.find((item) => item.value === range)?.label || "24 小时";

  const waveColumns: TableColumnProps<WaveReportRow>[] = [
    { title: "星系", dataIndex: "systemName", render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "开始", dataIndex: "startedAt", width: 154, render: (value?: string) => formatTime(value) },
    { title: "结束", dataIndex: "endedAt", width: 154, render: (value: string | undefined, row) => row.active ? <Tag color="red">进行中</Tag> : formatTime(value) },
    { title: "事件", dataIndex: "incidentCount", width: 70 },
    { title: "独立人员", dataIndex: "uniqueTargets", width: 84 },
  ];
  const systemColumns: TableColumnProps<SystemReportRow>[] = [
    { title: "星系", dataIndex: "name", width: 110, render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "来袭事件", dataIndex: "incidentCount", width: 84, sorter: (a, b) => a.incidentCount - b.incidentCount },
    { title: "目标人次", dataIndex: "targetSightings", width: 84 },
    { title: "独立人员", dataIndex: "uniqueTargets", width: 84 },
    { title: "最后出现", dataIndex: "lastSeen", render: (value?: string) => formatTime(value) },
  ];
  const targetColumns: TableColumnProps<TargetReportRow>[] = [
    { title: "已验证人员", dataIndex: "name", render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "威胁度", dataIndex: "dangerRatio", width: 104, sorter: (a, b) => (a.dangerRatio ?? -1) - (b.dangerRatio ?? -1), render: (value: number | null) => dangerTag(value) },
    { title: "出现批次", dataIndex: "incidentCount", width: 84, sorter: (a, b) => a.incidentCount - b.incidentCount },
    { title: "涉及星系", dataIndex: "systems", width: 122, render: (value: string[]) => textTokens(value) },
    { title: "击毁/损失", key: "combat", width: 118, render: (_: unknown, row) => <span className="combat-summary">{formatCompactNumber(row.zkill?.ships_destroyed)} / {formatCompactNumber(row.zkill?.ships_lost)}</span> },
    { title: "最后出现", dataIndex: "lastSeen", width: 154, render: (value?: string) => formatTime(value) },
  ];
  return (
    <div className="hostile-report-page">
      <header className="arco-page-header hostile-report-header">
        <div>
          <Typography.Text className="content-page-kicker">历史研判</Typography.Text>
          <Typography.Title heading={4}>来袭分析</Typography.Title>
        </div>
        <div className="hostile-report-actions">
          <Radio.Group aria-label="分析时间范围" type="button" value={range} onChange={(value) => setRange(value as ReportRange)}>
            {RANGE_OPTIONS.map((item) => <Radio key={item.value} value={item.value}>{item.label}</Radio>)}
          </Radio.Group>
          <Button icon={<IconRefresh />} loading={historyQuery.isFetching} type="outline" onClick={() => void historyQuery.refetch()}>刷新</Button>
        </div>
      </header>

      {historyQuery.isError ? (
        <Alert type="error" content={historyQuery.error instanceof Error ? historyQuery.error.message : "来袭历史加载失败"} />
      ) : null}

      <Grid.Row className="arco-summary-grid hostile-report-kpis" gutter={16}>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<ShieldAlert size={17} />} title="有效来袭事件" value={report.incidentCount} extra={<Typography.Text type="secondary">{activeRange}内已验证敌对</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<RadioTower size={17} />} title="来袭波次" value={report.waveCount} extra={<Typography.Text type="secondary">敌对出现至清空为一波</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<Users size={17} />} title="独立敌对人员" value={report.uniqueTargets} extra={<Typography.Text type="secondary">zKill 覆盖 {report.zkillCoverage.toFixed(0)}%</Typography.Text>} /></Card></Grid.Col>
        <Grid.Col lg={6} sm={12} xs={24}><Card><Statistic prefix={<MapPinned size={17} />} title="涉及星系" value={report.systemCount} extra={<Typography.Text type="secondary">跨星系人员 {report.crossSystemTargetCount}</Typography.Text>} /></Card></Grid.Col>
      </Grid.Row>

      <Grid.Row className="hostile-report-chart-grid" gutter={16}>
        <Grid.Col xl={14} lg={24} xs={24}>
          <Card className="hostile-report-card" title={<span><Activity size={16} />来袭趋势</span>} extra={<Tag color="green">{activeRange}</Tag>}>
            {report.incidentCount > 0 ? (
              <EveChart height={280} option={trendOption(report.trend.map((item) => item.label), report.trend.map((item) => item.count), theme)} />
            ) : <Empty description="所选范围内没有已验证敌对来袭" />}
          </Card>
        </Grid.Col>
        <Grid.Col xl={10} lg={24} xs={24}>
          <Card className="hostile-report-card" title={<span><RadioTower size={16} />最近来袭波次</span>} extra={<Typography.Text type="secondary">峰值 {report.peakWaveTargets} 人</Typography.Text>}>
            {report.waves.length > 0 ? (
              <Table<WaveReportRow> border={false} columns={waveColumns} data={report.waves.slice(0, 6)} pagination={false} rowKey="id" />
            ) : <Empty description="暂无可聚合波次" />}
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Grid.Row className="hostile-report-ranking-grid" gutter={16}>
        <Grid.Col xl={10} lg={24} xs={24}>
          <Card className="hostile-report-card" title={<span><MapPinned size={16} />热点星系</span>}>
            <Table<SystemReportRow> border={false} columns={systemColumns} data={report.systems.slice(0, 10)} pagination={false} rowKey="name" />
          </Card>
        </Grid.Col>
        <Grid.Col xl={14} lg={24} xs={24}>
          <Card className="hostile-report-card" title={<span><Crosshair size={16} />人员研判</span>} extra={<Typography.Text type="secondary">威胁度数据来自 zKillboard</Typography.Text>}>
            <Table<TargetReportRow> border={false} columns={targetColumns} data={report.targets.slice(0, 12)} pagination={false} rowKey="characterId" />
          </Card>
        </Grid.Col>
      </Grid.Row>

    </div>
  );
}
