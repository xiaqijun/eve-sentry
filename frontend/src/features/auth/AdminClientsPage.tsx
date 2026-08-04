import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Pagination,
  Select,
  Space,
  Tabs,
  Tag,
  Typography,
} from "@arco-design/web-react";
import {
  IconDesktop,
  IconEye,
  IconLock,
} from "@arco-design/web-react/icon";

import {
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
} from "../../components/ManagementPage";
import { deriveClientHealth } from "../clients/clientHealth";
import { listAdminClients } from "./api";
import type {
  AdminClientHeartbeatRecord,
  AdminClientKeyUsage,
  AdminClientOwner,
  AdminClientsSnapshot,
  ApiKeyRecord,
} from "./types";

type OnlineFilter = "all" | "online" | "offline";
const PAGE_SIZE = 20;

export interface AdminClientFilters {
  search: string;
  clientType: string;
  online: OnlineFilter;
  userId: string;
}

interface TargetRecord {
  character_name?: string;
  client_id?: string;
  last_error?: string;
  monitoring?: boolean;
  runtime_status?: string;
  source_instance?: string;
  system_name?: string;
  window_title?: string;
}

const EMPTY_SNAPSHOT: AdminClientsSnapshot = {
  clients: { count: 0, heartbeats: [] },
  keys: [],
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
}

function clientOwner(client: AdminClientHeartbeatRecord): AdminClientOwner | undefined {
  return client.owner || undefined;
}

function clientKey(client: AdminClientHeartbeatRecord): ApiKeyRecord | undefined {
  return client.key || undefined;
}

function ownerName(owner: AdminClientOwner | undefined, fallback = ""): string {
  if (!owner) return fallback || "未关联用户";
  return owner.display_name || owner.username || owner.user_id;
}

function keyName(key: ApiKeyRecord | undefined): string {
  return key?.name || "未关联密钥";
}

function keyPrefix(key: ApiKeyRecord | undefined): string {
  return key?.key_prefix || "";
}

function clientTargets(client: AdminClientHeartbeatRecord): TargetRecord[] {
  const targets = client.details?.targets;
  if (!Array.isArray(targets)) return [];
  return targets
    .map((target) => asRecord(target) as TargetRecord)
    .filter((target) => Object.keys(target).length > 0);
}

function targetLabel(target: TargetRecord): string {
  const identity = String(
    target.character_name
    || target.source_instance
    || target.window_title
    || target.client_id
    || "未命名目标",
  );
  const system = String(target.system_name || "未知星系");
  return `${identity} · ${system}`;
}

function clientSearchText(client: AdminClientHeartbeatRecord): string {
  const owner = clientOwner(client);
  const key = clientKey(client);
  const details = client.details || {};
  return [
    client.client_id,
    client.label,
    client.client_type,
    client.status,
    owner?.user_id,
    owner?.username,
    owner?.display_name,
    client.user_id,
    key?.name,
    key?.key_prefix,
    client.api_key_id,
    client.remote_ip,
    details.client_version,
    details.host,
    details.last_action,
    details.last_error,
    ...clientTargets(client).map(targetLabel),
  ].join(" ").toLocaleLowerCase();
}

export function filterAdminClients(
  clients: AdminClientHeartbeatRecord[],
  filters: AdminClientFilters,
): AdminClientHeartbeatRecord[] {
  const search = filters.search.trim().toLocaleLowerCase();
  return clients.filter((client) => {
    if (filters.clientType !== "all" && client.client_type !== filters.clientType) {
      return false;
    }
    if (filters.online === "online" && client.online !== true) return false;
    if (filters.online === "offline" && client.online === true) return false;
    const owner = clientOwner(client);
    const userId = owner?.user_id || client.user_id || "";
    if (filters.userId !== "all" && userId !== filters.userId) return false;
    return !search || clientSearchText(client).includes(search);
  });
}

function formatDate(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "从未";
  const date = new Date(text);
  return Number.isNaN(date.getTime())
    ? text
    : date.toLocaleString("zh-CN", { hour12: false });
}

function clientTypeLabel(value: unknown): string {
  const type = String(value || "client");
  return {
    detector_client: "监控端",
    alert_client: "预警端",
    channel_client: "频道端",
    integration_client: "集成端",
  }[type] || type;
}

function keyTypeLabel(value: ApiKeyRecord["key_type"]): string {
  return value === "desktop" ? "设备密钥" : "只读服务密钥";
}

function ClientStateTag({ client }: { client: AdminClientHeartbeatRecord }) {
  const health = deriveClientHealth(client).state;
  if (health === "warning") return <Tag color="red">异常</Tag>;
  if (client.online === true) return <Tag color="green">在线</Tag>;
  if (health === "stopped") return <Tag>已停止</Tag>;
  return <Tag color="orange">离线</Tag>;
}

function TargetSummary({ client }: { client: AdminClientHeartbeatRecord }) {
  const targets = clientTargets(client);
  const configuredCount = Number(client.details?.target_count);
  const count = targets.length || (Number.isFinite(configuredCount) ? configuredCount : 0);
  return (
    <span className="admin-client-target-summary">
      <strong>{count ? `${count} 个` : "无目标"}</strong>
      {targets[0] ? <small title={targetLabel(targets[0])}>{targetLabel(targets[0])}</small> : null}
    </span>
  );
}

function ClientOwnerCell({ client }: { client: AdminClientHeartbeatRecord }) {
  const owner = clientOwner(client);
  const key = clientKey(client);
  const prefix = keyPrefix(key);
  return (
    <span className="admin-client-stacked-cell">
      <strong title={ownerName(owner, client.user_id)}>{ownerName(owner, client.user_id)}</strong>
      <small title={`${keyName(key)} ${prefix}`.trim()}>
        {keyName(key)}{prefix ? ` · ${prefix}...` : ""}
      </small>
    </span>
  );
}

function ClientRuntimeCell({ client }: { client: AdminClientHeartbeatRecord }) {
  const lastError = String(client.details?.last_error || "").trim();
  const targetError = clientTargets(client).map((target) => String(target.last_error || "").trim()).find(Boolean);
  const error = lastError || targetError || "";
  const action = String(client.details?.last_action || "").trim();
  return (
    <span className={`admin-client-stacked-cell${error ? " has-error" : ""}`}>
      <strong title={error || action || "暂无动作"}>{error || action || "暂无动作"}</strong>
      {error && action ? <small title={action}>最后动作：{action}</small> : null}
    </span>
  );
}

function ClientRows({
  clients,
  loading,
  onOpen,
}: {
  clients: AdminClientHeartbeatRecord[];
  loading: boolean;
  onOpen: (client: AdminClientHeartbeatRecord) => void;
}) {
  if (!clients.length) {
    return <Empty description={loading ? "正在加载客户端" : "没有符合筛选条件的客户端"} />;
  }
  return (
    <div className="admin-client-list" role="table" aria-label="客户端实例">
      <div className="admin-client-row admin-client-row-head" role="row">
        <span>客户端</span><span>类型</span><span>所属用户 / 密钥</span><span>版本 / 主机</span>
        <span>状态</span><span>监控目标</span><span>最后动作 / 异常</span><span>最后心跳</span><span aria-hidden="true" />
      </div>
      {clients.map((client) => {
        const version = String(client.details?.client_version || "未知版本");
        const host = String(client.details?.host || "未知主机");
        return (
          <div className="admin-client-row" key={client.client_id} role="row">
            <span className="admin-client-stacked-cell" role="cell">
              <small className="admin-client-mobile-label">客户端</small>
              <strong title={client.label || client.client_id}>{client.label || "未命名客户端"}</strong>
              <small title={client.client_id}>{client.client_id}</small>
            </span>
            <span role="cell"><small className="admin-client-mobile-label">类型</small><Tag>{clientTypeLabel(client.client_type)}</Tag></span>
            <span role="cell"><small className="admin-client-mobile-label">所属用户 / 密钥</small><ClientOwnerCell client={client} /></span>
            <span className="admin-client-stacked-cell" role="cell">
              <small className="admin-client-mobile-label">版本 / 主机</small>
              <strong title={version}>{version}</strong><small title={host}>{host}</small>
            </span>
            <span role="cell"><small className="admin-client-mobile-label">状态</small><ClientStateTag client={client} /></span>
            <span role="cell"><small className="admin-client-mobile-label">监控目标</small><TargetSummary client={client} /></span>
            <span role="cell"><small className="admin-client-mobile-label">最后动作 / 异常</small><ClientRuntimeCell client={client} /></span>
            <span className="admin-client-time" role="cell">
              <small className="admin-client-mobile-label">最后心跳</small>
              <time dateTime={client.seen_at} title={formatDate(client.seen_at)}>{formatDate(client.seen_at)}</time>
            </span>
            <span className="admin-client-row-action" role="cell">
              <Button aria-label={`查看 ${client.label || client.client_id}`} icon={<IconEye />} shape="square" size="mini" title="查看详情" type="text" onClick={() => onOpen(client)} />
            </span>
          </div>
        );
      })}
    </div>
  );
}

function linkedClientCount(usage: AdminClientKeyUsage): number {
  const clientCount = Number(usage.client_count);
  if (Number.isFinite(clientCount)) return clientCount;
  if (Array.isArray(usage.linked_clients)) return usage.linked_clients.length;
  const value = Number(usage.linked_clients || 0);
  return Number.isFinite(value) ? value : 0;
}

function lastClientLabel(value: AdminClientKeyUsage["last_client"]): string {
  if (!value) return "无";
  if (typeof value === "string") return value;
  return value.label || value.client_id || "无";
}

function keySearchText(usage: AdminClientKeyUsage): string {
  return [
    usage.key.name,
    usage.key.key_prefix,
    usage.key.key_type,
    usage.key.status,
    usage.owner?.display_name,
    usage.owner?.username,
    usage.owner?.user_id,
    lastClientLabel(usage.last_client),
    usage.last_ip,
  ].join(" ").toLocaleLowerCase();
}

function KeyRows({ keys, loading }: { keys: AdminClientKeyUsage[]; loading: boolean }) {
  if (!keys.length) {
    return <Empty description={loading ? "正在加载密钥使用情况" : "没有符合筛选条件的密钥"} />;
  }
  return (
    <div className="admin-key-usage-list" role="table" aria-label="密钥使用">
      <div className="admin-key-usage-row admin-client-row-head" role="row">
        <span>名称 / 前缀</span><span>所属用户</span><span>类型</span><span>状态</span>
        <span>最后使用</span><span>关联客户端 / 在线</span><span>最后来源</span>
      </div>
      {keys.map((usage) => (
        <div className="admin-key-usage-row" key={usage.key.key_id} role="row">
          <span className="admin-client-stacked-cell" role="cell">
            <small className="admin-client-mobile-label">名称 / 前缀</small>
            <strong title={usage.key.name}>{usage.key.name || "未命名密钥"}</strong>
            <small>{usage.key.key_prefix}...</small>
          </span>
          <span className="admin-client-stacked-cell" role="cell"><small className="admin-client-mobile-label">所属用户</small><strong>{ownerName(usage.owner, usage.key.user_id)}</strong>{usage.owner?.username ? <small>@{usage.owner.username}</small> : null}</span>
          <span role="cell"><small className="admin-client-mobile-label">类型</small><Tag>{keyTypeLabel(usage.key.key_type)}</Tag></span>
          <span role="cell"><small className="admin-client-mobile-label">状态</small><Tag color={usage.key.status === "active" ? "green" : "red"}>{usage.key.status === "active" ? "有效" : "已吊销"}</Tag></span>
          <span className="admin-client-time" role="cell"><small className="admin-client-mobile-label">最后使用</small><time dateTime={usage.key.last_used_at}>{formatDate(usage.key.last_used_at)}</time></span>
          <span className="admin-key-counts" role="cell"><small className="admin-client-mobile-label">关联客户端 / 在线</small><strong>{linkedClientCount(usage)}</strong><small>{Number(usage.online_count || 0)} 在线</small></span>
          <span className="admin-client-stacked-cell" role="cell"><small className="admin-client-mobile-label">最后来源</small><strong title={lastClientLabel(usage.last_client)}>{lastClientLabel(usage.last_client)}</strong><small title={usage.last_ip || ""}>{usage.last_ip || "未记录 IP"}</small></span>
        </div>
      ))}
    </div>
  );
}

export function AdminClientsPage() {
  const [snapshot, setSnapshot] = useState<AdminClientsSnapshot>(EMPTY_SNAPSHOT);
  const [selectedClientId, setSelectedClientId] = useState<string>();
  const [search, setSearch] = useState("");
  const [clientType, setClientType] = useState("all");
  const [online, setOnline] = useState<OnlineFilter>("all");
  const [userId, setUserId] = useState("all");
  const [clientPage, setClientPage] = useState(1);
  const [keySearch, setKeySearch] = useState("");
  const [keyStatus, setKeyStatus] = useState("all");
  const [keyPage, setKeyPage] = useState(1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    setError("");
    try {
      const next = await listAdminClients();
      setSnapshot({
        clients: next.clients || EMPTY_SNAPSHOT.clients,
        keys: Array.isArray(next.keys) ? next.keys : [],
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "客户端管理数据加载失败");
    } finally {
      if (!background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const refreshTimer = window.setInterval(() => { void load(true); }, 15_000);
    return () => window.clearInterval(refreshTimer);
  }, [load]);

  const clients = snapshot.clients?.heartbeats || [];
  const selectedClient = clients.find((client) => client.client_id === selectedClientId);
  const filteredClients = useMemo(
    () => filterAdminClients(clients, { search, clientType, online, userId }),
    [clientType, clients, online, search, userId],
  );
  const filteredKeys = useMemo(() => {
    const query = keySearch.trim().toLocaleLowerCase();
    return snapshot.keys.filter((usage) => (
      (keyStatus === "all" || usage.key.status === keyStatus)
      && (!query || keySearchText(usage).includes(query))
    ));
  }, [keySearch, keyStatus, snapshot.keys]);
  const activeClientPage = Math.min(clientPage, Math.max(1, Math.ceil(filteredClients.length / PAGE_SIZE)));
  const activeKeyPage = Math.min(keyPage, Math.max(1, Math.ceil(filteredKeys.length / PAGE_SIZE)));
  const visibleClients = filteredClients.slice(
    (activeClientPage - 1) * PAGE_SIZE,
    activeClientPage * PAGE_SIZE,
  );
  const visibleKeys = filteredKeys.slice(
    (activeKeyPage - 1) * PAGE_SIZE,
    activeKeyPage * PAGE_SIZE,
  );

  useEffect(() => { setClientPage(1); }, [clientType, online, search, userId]);
  useEffect(() => { setKeyPage(1); }, [keySearch, keyStatus]);

  const clientTypes = Array.from(new Set(clients.map((client) => client.client_type || "client")));
  const userOptions = Array.from(new Map(clients.map((client) => {
    const owner = clientOwner(client);
    const id = owner?.user_id || client.user_id || "";
    return [id, { id, label: ownerName(owner, id) }] as const;
  }).filter(([id]) => Boolean(id))).values());
  const onlineCount = clients.filter((client) => client.online === true).length;
  const exceptionCount = clients.filter((client) => deriveClientHealth(client).isException).length;
  const targetCount = clients.reduce((total, client) => total + clientTargets(client).length, 0);

  const detailItems = selectedClient ? (() => {
    const owner = clientOwner(selectedClient);
    const key = clientKey(selectedClient);
    const targets = clientTargets(selectedClient);
    return [
      { label: "客户端 ID", value: <code>{selectedClient.client_id}</code> },
      { label: "类型", value: clientTypeLabel(selectedClient.client_type) },
      { label: "所属用户", value: ownerName(owner, selectedClient.user_id) },
      { label: "使用密钥", value: `${keyName(key)}${keyPrefix(key) ? ` · ${keyPrefix(key)}...` : ""}` },
      { label: "版本 / 主机", value: `${String(selectedClient.details?.client_version || "未知版本")} / ${String(selectedClient.details?.host || "未知主机")}` },
      { label: "状态", value: <ClientStateTag client={selectedClient} /> },
      { label: "最后动作", value: String(selectedClient.details?.last_action || "暂无") },
      { label: "最后异常", value: String(selectedClient.details?.last_error || "无") },
      { label: "最后心跳", value: formatDate(selectedClient.seen_at) },
      { label: "来源 IP", value: selectedClient.remote_ip || "未记录" },
      {
        label: "监控目标",
        value: targets.length ? (
          <Space direction="vertical" size={4}>
            {targets.map((target, index) => (
              <span className="admin-client-drawer-target" key={`${target.client_id || target.character_name || "target"}-${index}`}>
                <strong>{targetLabel(target)}</strong>
                <small>{target.runtime_status || (target.monitoring === false ? "未监控" : "监控中")}{target.last_error ? ` · ${target.last_error}` : ""}</small>
              </span>
            ))}
          </Space>
        ) : "无",
      },
    ];
  })() : [];

  return (
    <div className="admin-shell admin-clients-page">
      <ManagementPageHeader loading={loading} refreshLabel="刷新客户端" title="客户端管理" onRefresh={() => void load()} />
      <ManagementError error={error} />
      <ManagementSummary ariaLabel="客户端管理摘要" items={[
        { label: "客户端实例", value: clients.length },
        { label: "当前在线", value: onlineCount },
        { label: "异常客户端", value: exceptionCount },
        { label: "监控目标", value: targetCount },
      ]} />

      <Card className="arco-management-card admin-clients-card">
        <Tabs defaultActiveTab="clients">
          <Tabs.TabPane key="clients" title={<Space><IconDesktop />客户端实例</Space>}>
            <div className="admin-client-toolbar" aria-label="客户端筛选">
              <Input.Search aria-label="搜索客户端" allowClear placeholder="搜索客户端、用户、密钥、主机或目标" value={search} onChange={setSearch} />
              <Select aria-label="客户端类型" value={clientType} onChange={setClientType}>
                <Select.Option value="all">全部类型</Select.Option>
                {clientTypes.map((type) => <Select.Option key={type} value={type}>{clientTypeLabel(type)}</Select.Option>)}
              </Select>
              <Select aria-label="在线状态" value={online} onChange={(value) => setOnline(value as OnlineFilter)}>
                <Select.Option value="all">全部状态</Select.Option>
                <Select.Option value="online">在线</Select.Option>
                <Select.Option value="offline">离线</Select.Option>
              </Select>
              <Select aria-label="所属用户" value={userId} onChange={setUserId}>
                <Select.Option value="all">全部用户</Select.Option>
                {userOptions.map((option) => <Select.Option key={option.id} value={option.id}>{option.label}</Select.Option>)}
              </Select>
              <Typography.Text type="secondary">{filteredClients.length} / {clients.length} 个实例</Typography.Text>
            </div>
            <ClientRows clients={visibleClients} loading={loading} onOpen={(client) => setSelectedClientId(client.client_id)} />
            {filteredClients.length > PAGE_SIZE ? (
              <Pagination
                className="admin-client-pagination"
                current={activeClientPage}
                pageSize={PAGE_SIZE}
                total={filteredClients.length}
                onChange={setClientPage}
              />
            ) : null}
          </Tabs.TabPane>
          <Tabs.TabPane key="keys" title={<Space><IconLock />密钥使用</Space>}>
            <div className="admin-client-toolbar admin-key-toolbar" aria-label="密钥筛选">
              <Input.Search aria-label="搜索密钥使用" allowClear placeholder="搜索密钥、用户、客户端或来源 IP" value={keySearch} onChange={setKeySearch} />
              <Select aria-label="密钥状态" value={keyStatus} onChange={setKeyStatus}>
                <Select.Option value="all">全部状态</Select.Option>
                <Select.Option value="active">有效</Select.Option>
                <Select.Option value="revoked">已吊销</Select.Option>
              </Select>
              <Typography.Text type="secondary">{filteredKeys.length} / {snapshot.keys.length} 个密钥</Typography.Text>
            </div>
            <KeyRows keys={visibleKeys} loading={loading} />
            {filteredKeys.length > PAGE_SIZE ? (
              <Pagination
                className="admin-client-pagination"
                current={activeKeyPage}
                pageSize={PAGE_SIZE}
                total={filteredKeys.length}
                onChange={setKeyPage}
              />
            ) : null}
          </Tabs.TabPane>
        </Tabs>
      </Card>

      <Drawer
        className="management-drawer admin-client-drawer"
        footer={null}
        title={selectedClient ? selectedClient.label || "客户端详情" : "客户端详情"}
        visible={Boolean(selectedClient)}
        width={580}
        onCancel={() => setSelectedClientId(undefined)}
      >
        {selectedClient ? <Descriptions border column={1} data={detailItems} size="small" /> : null}
      </Drawer>
    </div>
  );
}
