import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Radio,
  Table,
  Tag,
  Typography,
  type TableColumnProps,
} from "@arco-design/web-react";
import { IconRefresh } from "@arco-design/web-react/icon";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";

import type { AlertItem, VerifiedCharacter } from "../workbench/types";
import { fetchHostileAlertHistory } from "./api";
import {
  buildHostileReport,
  type ReportRange,
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

function textTokens(values: string[], fallback = "-") {
  const clean = values.map((value) => String(value).trim()).filter(Boolean);
  if (clean.length === 0) return fallback;
  return (
    <span className="table-token-list">
      {clean.map((value, index) => <span key={`${value}:${index}`}>{value}</span>)}
    </span>
  );
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

export function HostileHistoryPage() {
  const [range, setRange] = useState<ReportRange>("24h");
  const [historySearch, setHistorySearch] = useState("");
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
  const historyNeedle = historySearch.trim().toLocaleLowerCase();
  const filteredWaves = useMemo(() => {
    if (!historyNeedle) return report.waves;
    return report.waves.filter((wave) => (
      `${wave.systemName} ${wave.id}`.toLocaleLowerCase().includes(historyNeedle)
    ));
  }, [historyNeedle, report.waves]);
  const filteredRecent = useMemo(() => {
    if (!historyNeedle) return report.recent;
    return report.recent.filter((alert) => {
      const characters = (alert.verified_characters || []).map((item) => item.name);
      return [
        alert.system_name,
        alert.system,
        ...(alert.names || []),
        ...characters,
      ].some((value) => String(value || "").toLocaleLowerCase().includes(historyNeedle));
    });
  }, [historyNeedle, report.recent]);

  const waveColumns: TableColumnProps<WaveReportRow>[] = [
    { title: "星系", dataIndex: "systemName", render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "开始", dataIndex: "startedAt", width: 154, render: (value?: string) => formatTime(value) },
    { title: "结束", dataIndex: "endedAt", width: 154, render: (value: string | undefined, row) => row.active ? <Tag color="red">进行中</Tag> : formatTime(value) },
    { title: "事件", dataIndex: "incidentCount", width: 70 },
    { title: "独立人员", dataIndex: "uniqueTargets", width: 84 },
  ];
  const recentColumns: TableColumnProps<AlertItem>[] = [
    { title: "时间", dataIndex: "created_at", width: 154, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 110, render: (value?: string) => value || "未知星系" },
    { title: "已验证人员", dataIndex: "verified_characters", render: (value?: VerifiedCharacter[]) => textTokens((value || []).map((item) => item.name)) },
    { title: "级别", dataIndex: "level", width: 76, render: (value?: string) => levelTag(value) },
  ];

  return (
    <div className="hostile-report-page hostile-history-page">
      <header className="arco-page-header hostile-report-header">
        <div>
          <Typography.Text className="content-page-kicker">历史研判</Typography.Text>
          <Typography.Title heading={4}>来袭历史</Typography.Title>
          <Typography.Text type="secondary">查询红色图标来袭波次，以及 OCR 增效和身份核验产生的人员告警。</Typography.Text>
        </div>
        <div className="hostile-report-actions">
          <Radio.Group aria-label="查询时间范围" type="button" value={range} onChange={(value) => setRange(value as ReportRange)}>
            {RANGE_OPTIONS.map((item) => <Radio key={item.value} value={item.value}>{item.label}</Radio>)}
          </Radio.Group>
          <Button icon={<IconRefresh />} loading={historyQuery.isFetching} type="outline" onClick={() => void historyQuery.refetch()}>刷新</Button>
        </div>
      </header>

      {historyQuery.isError ? (
        <Alert type="error" content={historyQuery.error instanceof Error ? historyQuery.error.message : "来袭历史加载失败"} />
      ) : null}

      <Card
        className="hostile-report-card hostile-report-recent"
        title={<span><ShieldAlert size={16} />历史记录</span>}
        extra={<Input.Search
          allowClear
          placeholder="搜索星系或人员"
          value={historySearch}
          onChange={setHistorySearch}
        />}
      >
        <Typography.Text type="secondary">
          当前范围：{activeRange}，更新于 {formatTime(historyQuery.data?.generatedAt)}。历史数据按服务器返回的有界范围展示。
        </Typography.Text>
        <Typography.Title heading={6}>来袭波次（{filteredWaves.length}）</Typography.Title>
        {filteredWaves.length > 0 ? (
          <Table<WaveReportRow>
            border={false}
            columns={waveColumns}
            data={filteredWaves}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            rowKey="id"
          />
        ) : <Empty description="没有匹配的来袭波次" />}
        <Typography.Title heading={6}>人员告警（{filteredRecent.length}）</Typography.Title>
        {filteredRecent.length > 0 ? (
          <Table<AlertItem>
            border={false}
            columns={recentColumns}
            data={filteredRecent}
            pagination={{ pageSize: 8, hideOnSinglePage: true }}
            rowKey="id"
          />
        ) : <Empty description="没有匹配的人员告警" />}
      </Card>
    </div>
  );
}
