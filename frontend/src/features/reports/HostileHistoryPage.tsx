import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Grid,
  Input,
  Pagination,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  type TableColumnProps,
} from "@arco-design/web-react";
import {
  IconEye,
  IconFilter,
  IconList,
  IconRefresh,
  IconSearch,
} from "@arco-design/web-react/icon";
import { useQuery } from "@tanstack/react-query";

import {
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
} from "../../components/ManagementPage";
import type { AlertItem, VerifiedCharacter } from "../workbench/types";
import { fetchHostileAlertHistory } from "./api";
import {
  buildHostileReport,
  type ReportRange,
  type WaveReportRow,
} from "./reporting";

const REPORT_REFRESH_INTERVAL_MS = 60000;
const DEFAULT_PAGE_SIZE = 10;

type HistoryView = "waves" | "alerts";
type HistoryStatus = "all" | "active" | "cleared" | "critical" | "high" | "medium" | "low";

interface HistoryQueryFilters {
  range: ReportRange;
  keyword: string;
  system: string;
  status: HistoryStatus;
}

type SelectedRecord =
  | { kind: "wave"; value: WaveReportRow }
  | { kind: "alert"; value: AlertItem };

const DEFAULT_FILTERS: HistoryQueryFilters = {
  range: "24h",
  keyword: "",
  system: "all",
  status: "all",
};

const RANGE_OPTIONS: Array<{ value: ReportRange; label: string }> = [
  { value: "24h", label: "近 24 小时" },
  { value: "7d", label: "近 7 天" },
  { value: "30d", label: "近 30 天" },
  { value: "all", label: "全部时间" },
];

function formatTime(value?: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(start?: string, end?: string): string {
  const startMs = start ? new Date(start).getTime() : Number.NaN;
  const endMs = end ? new Date(end).getTime() : Date.now();
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return "-";
  const totalMinutes = Math.max(0, Math.round((endMs - startMs) / 60000));
  if (totalMinutes < 60) return `${totalMinutes} 分钟`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`;
}

function cleanSystem(alert: AlertItem): string {
  return String(alert.system_name || alert.system || "未知星系").trim() || "未知星系";
}

function verifiedNames(alert: AlertItem): string[] {
  const verified = (alert.verified_characters || [])
    .map((item) => String(item.name || "").trim())
    .filter(Boolean);
  if (verified.length > 0) return verified;
  return (alert.names || []).map((name) => String(name).trim()).filter(Boolean);
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
  const [view, setView] = useState<HistoryView>("waves");
  const [draftFilters, setDraftFilters] = useState<HistoryQueryFilters>(DEFAULT_FILTERS);
  const [queryFilters, setQueryFilters] = useState<HistoryQueryFilters>(DEFAULT_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [selectedRecord, setSelectedRecord] = useState<SelectedRecord | null>(null);

  const historyQuery = useQuery({
    queryKey: ["hostile-alert-history", queryFilters.range],
    queryFn: () => fetchHostileAlertHistory(queryFilters.range),
    refetchInterval: REPORT_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });
  const report = useMemo(
    () => buildHostileReport(
      historyQuery.data?.alerts || [],
      queryFilters.range,
      Date.now(),
      historyQuery.data?.waves || [],
    ),
    [historyQuery.data?.alerts, historyQuery.data?.waves, queryFilters.range],
  );
  const systems = useMemo(() => Array.from(new Set([
    ...report.waves.map((wave) => wave.systemName),
    ...report.recent.map(cleanSystem),
  ])).sort((left, right) => left.localeCompare(right)), [report.recent, report.waves]);

  const keyword = queryFilters.keyword.trim().toLocaleLowerCase();
  const filteredWaves = useMemo(() => report.waves.filter((wave) => {
    const matchesKeyword = !keyword
      || `${wave.systemName} ${wave.id}`.toLocaleLowerCase().includes(keyword);
    const matchesSystem = queryFilters.system === "all" || wave.systemName === queryFilters.system;
    const matchesStatus = queryFilters.status === "all"
      || (queryFilters.status === "active" && wave.active)
      || (queryFilters.status === "cleared" && !wave.active);
    return matchesKeyword && matchesSystem && matchesStatus;
  }), [keyword, queryFilters.status, queryFilters.system, report.waves]);
  const filteredAlerts = useMemo(() => report.recent.filter((alert) => {
    const matchesKeyword = !keyword || [
      alert.id,
      cleanSystem(alert),
      ...verifiedNames(alert),
    ].some((value) => String(value || "").toLocaleLowerCase().includes(keyword));
    const matchesSystem = queryFilters.system === "all" || cleanSystem(alert) === queryFilters.system;
    const matchesStatus = queryFilters.status === "all" || alert.level === queryFilters.status;
    return matchesKeyword && matchesSystem && matchesStatus;
  }), [keyword, queryFilters.status, queryFilters.system, report.recent]);

  const currentTotal = view === "waves" ? filteredWaves.length : filteredAlerts.length;
  const pagedWaves = filteredWaves.slice((page - 1) * pageSize, page * pageSize);
  const pagedAlerts = filteredAlerts.slice((page - 1) * pageSize, page * pageSize);
  const activeWaveCount = report.waves.filter((wave) => wave.active).length;
  const rangeLabel = RANGE_OPTIONS.find((item) => item.value === queryFilters.range)?.label || "近 24 小时";

  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(currentTotal / pageSize));
    setPage((current) => Math.min(current, lastPage));
  }, [currentTotal, pageSize]);

  const runQuery = () => {
    setQueryFilters({ ...draftFilters, keyword: draftFilters.keyword.trim() });
    setPage(1);
  };
  const resetQuery = () => {
    setDraftFilters(DEFAULT_FILTERS);
    setQueryFilters(DEFAULT_FILTERS);
    setPage(1);
    setPageSize(DEFAULT_PAGE_SIZE);
  };
  const changeView = (key: string) => {
    const nextView = key as HistoryView;
    setView(nextView);
    setDraftFilters((current) => ({ ...current, status: "all" }));
    setQueryFilters((current) => ({ ...current, status: "all" }));
    setPage(1);
  };

  const waveColumns: TableColumnProps<WaveReportRow>[] = [
    { title: "序号", width: 64, align: "center", render: (_value, _row, index) => (page - 1) * pageSize + index + 1 },
    { title: "星系", dataIndex: "systemName", width: 140, render: (value: string) => <Typography.Text bold>{value}</Typography.Text> },
    { title: "开始时间", dataIndex: "startedAt", width: 170, render: (value?: string) => formatTime(value) },
    { title: "结束时间", dataIndex: "endedAt", width: 170, render: (value: string | undefined, row) => row.active ? "-" : formatTime(value) },
    { title: "持续时间", width: 130, render: (_value, row) => formatDuration(row.startedAt, row.active ? undefined : row.endedAt) },
    { title: "事件数", dataIndex: "incidentCount", width: 90, align: "right" },
    { title: "独立人员", dataIndex: "uniqueTargets", width: 100, align: "right" },
    { title: "状态", width: 90, align: "center", render: (_value, row) => <Tag color={row.active ? "red" : "green"}>{row.active ? "进行中" : "已清空"}</Tag> },
    { title: "操作", width: 84, align: "center", render: (_value, row) => <Button icon={<IconEye />} size="mini" type="text" onClick={() => setSelectedRecord({ kind: "wave", value: row })}>查看</Button> },
  ];
  const alertColumns: TableColumnProps<AlertItem>[] = [
    { title: "序号", width: 64, align: "center", render: (_value, _row, index) => (page - 1) * pageSize + index + 1 },
    { title: "告警时间", dataIndex: "created_at", width: 170, render: (value?: string) => formatTime(value) },
    { title: "星系", dataIndex: "system_name", width: 140, render: (_value, row) => <Typography.Text bold>{cleanSystem(row)}</Typography.Text> },
    { title: "已验证人员", dataIndex: "verified_characters", render: (_value: VerifiedCharacter[] | undefined, row) => textTokens(verifiedNames(row)) },
    { title: "级别", dataIndex: "level", width: 90, align: "center", render: (value?: string) => levelTag(value) },
    { title: "来源", width: 120, render: () => <Tag color="arcoblue">OCR / 身份核验</Tag> },
    { title: "操作", width: 84, align: "center", render: (_value, row) => <Button icon={<IconEye />} size="mini" type="text" onClick={() => setSelectedRecord({ kind: "alert", value: row })}>查看</Button> },
  ];

  const waveDetails = selectedRecord?.kind === "wave" ? [
    { label: "记录 ID", value: <Typography.Text copyable>{selectedRecord.value.id}</Typography.Text> },
    { label: "星系", value: selectedRecord.value.systemName },
    { label: "状态", value: <Tag color={selectedRecord.value.active ? "red" : "green"}>{selectedRecord.value.active ? "进行中" : "已清空"}</Tag> },
    { label: "开始时间", value: formatTime(selectedRecord.value.startedAt) },
    { label: "最后发现", value: formatTime(selectedRecord.value.lastSeen) },
    { label: "结束时间", value: selectedRecord.value.active ? "-" : formatTime(selectedRecord.value.endedAt) },
    { label: "持续时间", value: formatDuration(selectedRecord.value.startedAt, selectedRecord.value.active ? undefined : selectedRecord.value.endedAt) },
    { label: "告警事件", value: selectedRecord.value.incidentCount },
    { label: "独立人员", value: selectedRecord.value.uniqueTargets },
  ] : [];
  const alertDetails = selectedRecord?.kind === "alert" ? [
    { label: "记录 ID", value: <Typography.Text copyable>{selectedRecord.value.id}</Typography.Text> },
    { label: "告警时间", value: formatTime(selectedRecord.value.created_at) },
    { label: "星系", value: cleanSystem(selectedRecord.value) },
    { label: "告警级别", value: levelTag(selectedRecord.value.level) },
    { label: "已验证人员", value: verifiedNames(selectedRecord.value).join("、") || "-" },
    { label: "角色 ID", value: (selectedRecord.value.character_ids || []).join("、") || "-" },
    { label: "分类", value: selectedRecord.value.classification === "red" ? "红色图标敌对" : String(selectedRecord.value.classification || "-") },
  ] : [];

  return (
    <div className="admin-shell hostile-history-page">
      <ManagementPageHeader
        extra={<Tag color="green">每 60 秒自动刷新</Tag>}
        loading={historyQuery.isFetching}
        refreshLabel="刷新数据"
        title="来袭历史查询"
        onRefresh={() => void historyQuery.refetch()}
      />
      <ManagementError error={historyQuery.isError
        ? historyQuery.error instanceof Error ? historyQuery.error.message : "来袭历史加载失败"
        : ""}
      />
      <ManagementSummary ariaLabel="来袭历史摘要" items={[
        { label: "来袭波次", value: report.waves.length },
        { label: "人员告警", value: report.recent.length },
        { label: "涉及星系", value: systems.length },
        { label: "进行中波次", value: activeWaveCount },
      ]} />

      <Card className="hostile-history-filter-card arco-management-card" title={<Space><IconFilter />查询条件</Space>}>
        <Form className="hostile-history-filter-form" layout="vertical">
          <Grid.Row gutter={16}>
            <Grid.Col lg={5} md={8} sm={12} xs={24}>
              <Form.Item label="时间范围">
                <Select value={draftFilters.range} onChange={(value) => setDraftFilters((current) => ({ ...current, range: value as ReportRange }))}>
                  {RANGE_OPTIONS.map((item) => <Select.Option key={item.value} value={item.value}>{item.label}</Select.Option>)}
                </Select>
              </Form.Item>
            </Grid.Col>
            <Grid.Col lg={5} md={8} sm={12} xs={24}>
              <Form.Item label="星系">
                <Select showSearch value={draftFilters.system} onChange={(value) => setDraftFilters((current) => ({ ...current, system: String(value) }))}>
                  <Select.Option value="all">全部星系</Select.Option>
                  {systems.map((system) => <Select.Option key={system} value={system}>{system}</Select.Option>)}
                </Select>
              </Form.Item>
            </Grid.Col>
            <Grid.Col lg={5} md={8} sm={12} xs={24}>
              <Form.Item label={view === "waves" ? "波次状态" : "告警级别"}>
                <Select value={draftFilters.status} onChange={(value) => setDraftFilters((current) => ({ ...current, status: value as HistoryStatus }))}>
                  <Select.Option value="all">全部</Select.Option>
                  {view === "waves" ? (
                    <>
                      <Select.Option value="active">进行中</Select.Option>
                      <Select.Option value="cleared">已清空</Select.Option>
                    </>
                  ) : (
                    <>
                      <Select.Option value="critical">紧急</Select.Option>
                      <Select.Option value="high">高</Select.Option>
                      <Select.Option value="medium">中</Select.Option>
                      <Select.Option value="low">低</Select.Option>
                    </>
                  )}
                </Select>
              </Form.Item>
            </Grid.Col>
            <Grid.Col lg={9} md={24} sm={24} xs={24}>
              <Form.Item label="关键词">
                <Input
                  allowClear
                  placeholder={view === "waves" ? "输入星系或波次 ID" : "输入星系、人员或告警 ID"}
                  value={draftFilters.keyword}
                  onChange={(value) => setDraftFilters((current) => ({ ...current, keyword: value }))}
                  onPressEnter={runQuery}
                />
              </Form.Item>
            </Grid.Col>
          </Grid.Row>
          <div className="hostile-history-filter-actions">
            <Button icon={<IconSearch />} loading={historyQuery.isFetching} type="primary" onClick={runQuery}>查询</Button>
            <Button icon={<IconRefresh />} onClick={resetQuery}>重置</Button>
          </div>
        </Form>
      </Card>

      <Card
        className="hostile-history-result-card arco-management-card"
        extra={<Typography.Text type="secondary">数据更新于 {formatTime(historyQuery.data?.generatedAt)}</Typography.Text>}
        title={<Space><IconList />查询结果</Space>}
      >
        <Tabs activeTab={view} onChange={changeView}>
          <Tabs.TabPane key="waves" title={`来袭波次（${filteredWaves.length}）`}>
            <div className="hostile-history-result-meta">
              <Typography.Text type="secondary">查询范围：{rangeLabel}，共 {filteredWaves.length} 条记录</Typography.Text>
            </div>
            {pagedWaves.length > 0 ? (
              <Table<WaveReportRow> border={false} columns={waveColumns} data={pagedWaves} loading={historyQuery.isFetching} pagination={false} rowKey="id" />
            ) : <Empty description={historyQuery.isFetching ? "正在查询来袭波次" : "没有符合条件的来袭波次"} />}
          </Tabs.TabPane>
          <Tabs.TabPane key="alerts" title={`人员告警（${filteredAlerts.length}）`}>
            <div className="hostile-history-result-meta">
              <Typography.Text type="secondary">查询范围：{rangeLabel}，共 {filteredAlerts.length} 条记录</Typography.Text>
            </div>
            {pagedAlerts.length > 0 ? (
              <Table<AlertItem> border={false} columns={alertColumns} data={pagedAlerts} loading={historyQuery.isFetching} pagination={false} rowKey="id" />
            ) : <Empty description={historyQuery.isFetching ? "正在查询人员告警" : "没有符合条件的人员告警"} />}
          </Tabs.TabPane>
        </Tabs>
        <div className="hostile-history-pagination">
          <Pagination
            current={page}
            pageSize={pageSize}
            showJumper
            showTotal
            sizeCanChange
            sizeOptions={[10, 20, 50]}
            total={currentTotal}
            onChange={(nextPage, nextSize) => {
              setPage(nextPage);
              if (nextSize !== pageSize) setPageSize(nextSize);
            }}
            onPageSizeChange={(nextSize) => {
              setPageSize(nextSize);
              setPage(1);
            }}
          />
        </div>
      </Card>

      <Drawer
        footer={null}
        title={selectedRecord?.kind === "wave" ? "来袭波次详情" : "人员告警详情"}
        visible={Boolean(selectedRecord)}
        width={520}
        onCancel={() => setSelectedRecord(null)}
      >
        {selectedRecord ? (
          <Descriptions
            border
            column={1}
            data={selectedRecord.kind === "wave" ? waveDetails : alertDetails}
            size="small"
          />
        ) : null}
      </Drawer>
    </div>
  );
}
