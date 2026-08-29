import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Card,
  Descriptions,
  Grid,
  Progress,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { IconCloud, IconRefresh } from "@arco-design/web-react/icon";

import {
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
} from "../../components/ManagementPage";
import { fetchEsiGateway } from "./api";
import type { EsiGatewayHealth, EsiGatewaySnapshot } from "./types";

const EMPTY_SNAPSHOT: EsiGatewaySnapshot = {
  gateway: { configured: false, reachable: false },
  resolver_cache: {},
  client_metrics: {},
};

const CACHE_NAMESPACE_LABELS: Record<string, string> = {
  name: "人员名称",
  character: "角色资料",
  corporation: "军团资料",
  alliance: "联盟资料",
  system: "星系资料",
};

function numberValue(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function percentValue(value: unknown): number {
  return Math.round(numberValue(value) * 100);
}

function formatUptime(value: unknown): string {
  const seconds = Math.max(0, Math.floor(numberValue(value)));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return `${hours} 小时${remainder ? ` ${remainder} 分钟` : ""}`;
}

function formatCheckedAt(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "未知";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? text : date.toLocaleString("zh-CN", { hour12: false });
}

function gatewayState(snapshot: EsiGatewaySnapshot): { label: string; color: "green" | "orange" | "red" | "gray" } {
  if (!snapshot.gateway.configured) return { label: "未启用远端网关", color: "gray" };
  if (snapshot.gateway.reachable) return { label: "网关在线", color: "green" };
  return { label: "网关不可达", color: "red" };
}

function healthOf(snapshot: EsiGatewaySnapshot): EsiGatewayHealth {
  return snapshot.gateway.health || {};
}

export function AdminEsiGatewayPage() {
  const [snapshot, setSnapshot] = useState<EsiGatewaySnapshot>(EMPTY_SNAPSHOT);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    setError("");
    try {
      const next = await fetchEsiGateway();
      setSnapshot((current) => {
        const keepLastHealth = next.gateway.configured
          && !next.gateway.reachable
          && current.gateway.configured
          && current.gateway.health;
        return keepLastHealth
          ? {
            ...next,
            gateway: { ...next.gateway, health: current.gateway.health },
            client_metrics: Object.keys(next.client_metrics || {}).length
              ? next.client_metrics
              : current.client_metrics,
          }
          : next;
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "ESI 网关观测数据加载失败");
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(true); }, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const health = healthOf(snapshot);
  const state = gatewayState(snapshot);
  const resolverTotals = snapshot.resolver_cache?.totals || {};
  const personnelCache = snapshot.resolver_cache?.personnel;
  const nameNamespaceCache = snapshot.resolver_cache?.namespaces?.name;
  const resolverCacheRows = useMemo(() => Object.entries(snapshot.resolver_cache?.namespaces || {})
    .map(([namespace, values]) => ({
      key: namespace,
      namespace: CACHE_NAMESPACE_LABELS[namespace] || namespace,
      lookups: numberValue(values.lookups),
      hits: numberValue(values.hits),
      misses: numberValue(values.misses),
      staleHits: numberValue(values.stale_hits),
      hitRate: `${percentValue(values.hit_rate)}%`,
      activeEntries: numberValue(values.active_entries),
      staleEntries: numberValue(values.stale_entries),
    }))
    .sort((a, b) => b.lookups - a.lookups), [snapshot.resolver_cache?.namespaces]);
  const latencyRows = useMemo(() => Object.entries(
    snapshot.client_metrics.endpoints || snapshot.client_metrics.durations_ms || {},
  )
    .map(([endpoint, values]) => ({
      key: endpoint,
      endpoint,
      count: numberValue("requests" in values ? values.requests : values.count),
      hits: numberValue("cache_hits" in values ? values.cache_hits : 0),
      misses: numberValue("cache_misses" in values ? values.cache_misses : 0),
      last: numberValue("last_ms" in values
        ? values.last_ms
        : "last_latency_ms" in values ? values.last_latency_ms : values.last),
      p50: numberValue("p50_ms" in values ? values.p50_ms : values.p50),
      p95: numberValue("p95_ms" in values ? values.p95_ms : values.p95),
    }))
    .sort((a, b) => b.count - a.count), [snapshot.client_metrics.endpoints, snapshot.client_metrics.durations_ms]);
  const upstreamRequestCount = numberValue(health.upstream_requests ?? health.requests);
  const cacheMisses = numberValue(health.cache_misses ?? upstreamRequestCount);
  const cacheRate = percentValue(health.cache_hit_rate);
  const requestRate = numberValue(health.request_rate_per_second);
  const nameLookups = numberValue(
    personnelCache?.lookups ?? nameNamespaceCache?.lookups ?? resolverTotals.lookups,
  );
  const nameHits = numberValue(
    personnelCache?.hits ?? nameNamespaceCache?.hits ?? resolverTotals.hits,
  );
  const nameMisses = numberValue(
    personnelCache?.misses ?? nameNamespaceCache?.misses ?? resolverTotals.misses,
  );
  const nameHitRate = percentValue(
    personnelCache?.hit_rate ?? nameNamespaceCache?.hit_rate ?? resolverTotals.hit_rate,
  );
  const resolverEntries = snapshot.resolver_cache?.entries || {};

  return (
    <div className="admin-shell admin-esi-gateway-page">
      <ManagementPageHeader
        loading={loading}
        refreshLabel="刷新观测"
        title="ESI 网关观测"
        subtitle="查看 47 网关与 114 远端 ESI 客户端的实时运行摘要"
        onRefresh={() => void load()}
      />
      <ManagementError error={error} />

      <ManagementSummary ariaLabel="ESI 网关摘要" items={[
        { label: "114 名单查询", value: nameLookups },
        { label: "114 缓存命中", value: nameHits },
        { label: "114 缓存未命中", value: nameMisses },
        { label: "114 名单命中率", value: `${nameHitRate}%` },
        { label: "Gateway 上游请求", value: upstreamRequestCount },
      ]} />

      <Card
        className="arco-management-card"
        title="114 业务缓存"
        extra={<Typography.Text type="secondary">按单个人员或资料查询统计</Typography.Text>}
      >
        <Space direction="vertical" size={18} style={{ width: "100%" }}>
          <Descriptions
            border
            column={3}
            data={[
              { label: "名单新鲜命中", value: numberValue(personnelCache?.fresh_hits) },
              { label: "名单过期命中", value: numberValue(personnelCache?.stale_hits) },
              { label: "名单负缓存命中", value: numberValue(personnelCache?.negative_hits) },
              {
                label: "近 60 秒名单查询率",
                value: `${numberValue(personnelCache?.lookup_rate_per_second).toFixed(2)} req/s`,
              },
              { label: "有效缓存条目", value: numberValue(resolverEntries.active) },
              { label: "过期缓存条目", value: numberValue(resolverEntries.stale) },
            ]}
            size="small"
          />
          <Table
            border={false}
            columns={[
              { title: "缓存类型", dataIndex: "namespace" },
              { title: "查询", dataIndex: "lookups" },
              { title: "命中", dataIndex: "hits" },
              { title: "未命中", dataIndex: "misses" },
              { title: "使用过期值", dataIndex: "staleHits" },
              { title: "命中率", dataIndex: "hitRate" },
              { title: "有效条目", dataIndex: "activeEntries" },
              { title: "过期条目", dataIndex: "staleEntries" },
            ]}
            data={resolverCacheRows}
            loading={loading}
            noDataElement="暂无 114 ESI 缓存记录"
            pagination={false}
            rowKey="key"
          />
        </Space>
      </Card>

      <Grid.Row gutter={16}>
        <Grid.Col flex="1" xs={24} md={14}>
          <Card
            className="arco-management-card"
            title={<Space><IconCloud /><span>Gateway 状态</span></Space>}
            extra={<Tag color={state.color}>{state.label}</Tag>}
          >
            {!snapshot.gateway.configured ? (
              <Alert content="当前服务使用本地 ESI 后端，未配置远端 Gateway。" type="info" />
            ) : (
              <Space direction="vertical" size={18} style={{ width: "100%" }}>
                {!snapshot.gateway.reachable ? (
                  <Alert content={snapshot.gateway.error || "Gateway 健康检查失败，请检查 47 主机和 ZeroTier 网络。"} type="error" />
                ) : null}
                <Descriptions
                  border
                  column={1}
                  data={[
                    { label: "服务", value: health.service || "eve-sentry-esi-gateway" },
                    { label: "版本", value: health.version || "未知" },
                    { label: "地址", value: snapshot.gateway.url || "未配置" },
                    { label: "运行时间", value: formatUptime(health.uptime_seconds) },
                    { label: "近 60 秒请求率", value: `${requestRate.toFixed(2)} req/s` },
                    { label: "最后检查", value: formatCheckedAt(snapshot.gateway.checked_at) },
                    { label: "限流", value: `${numberValue(health.rate_limit_per_second)} req/s` },
                  ]}
                  size="small"
                />
              </Space>
            )}
          </Card>
        </Grid.Col>
        <Grid.Col flex="1" xs={24} md={10}>
          <Card className="arco-management-card" title="延迟与缓存">
            <Space direction="vertical" size={20} style={{ width: "100%" }}>
              <Grid.Row gutter={16}>
                <Grid.Col span={12}><Statistic title="最近上游延迟" value={numberValue(health.latency_ms?.last)} suffix="ms" /></Grid.Col>
                <Grid.Col span={12}><Statistic title="平均上游延迟" value={numberValue(health.latency_ms?.average)} suffix="ms" /></Grid.Col>
              </Grid.Row>
              <div>
                <Typography.Text type="secondary">缓存命中率</Typography.Text>
                <Progress percent={cacheRate} showText />
              </div>
              <Typography.Text type="secondary">当前缓存条目：{numberValue(health.cache_entries)}</Typography.Text>
              <Typography.Text type="secondary">缓存未命中：{cacheMisses}</Typography.Text>
            </Space>
          </Card>
        </Grid.Col>
      </Grid.Row>

      <Card
        className="arco-management-card"
        title="114 远端客户端请求"
        extra={<Typography.Text type="secondary">最近 1000 次调用的分位延迟</Typography.Text>}
      >
        <Table
          border={false}
          columns={[
            { title: "端点", dataIndex: "endpoint" },
            { title: "次数", dataIndex: "count" },
            { title: "命中", dataIndex: "hits" },
            { title: "未命中", dataIndex: "misses" },
            { title: "最近 (ms)", dataIndex: "last" },
            { title: "P50 (ms)", dataIndex: "p50" },
            { title: "P95 (ms)", dataIndex: "p95" },
          ]}
          data={latencyRows}
          loading={loading}
          noDataElement="暂无远端 ESI 请求记录"
          pagination={false}
          rowKey="key"
        />
      </Card>
    </div>
  );
}
