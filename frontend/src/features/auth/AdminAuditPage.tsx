import { useEffect, useMemo, useState } from "react";
import {
  Card,
  Empty,
  Input,
  List,
  Pagination,
  Select,
  Space,
  Tag,
  Typography,
} from "@arco-design/web-react";
import { IconExclamationCircle, IconFile, IconUser } from "@arco-design/web-react/icon";

import {
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
} from "../../components/ManagementPage";
import { deriveClientHealth } from "../clients/clientHealth";
import { fetchClients, listAudit } from "./api";
import type { AuditRecord, ClientHeartbeatRecord } from "./types";

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "api_key.created": "创建密钥",
  "api_key.deleted": "删除密钥",
  "api_key.enabled": "启用密钥",
  "api_key.revoked": "吊销密钥",
  "character.whitelist_removed": "移除角色白名单",
  "character.whitelisted": "添加角色白名单",
  "corporation.allowed": "添加允许军团",
  "corporation.removed": "移除允许军团",
  "identity.check_failed": "身份检查异常",
  "identity.user_disabled": "身份违规禁用用户",
  "identity.key_revoked": "违规角色吊销密钥",
  "identity.desktop_keys_revoked": "授权变更吊销设备密钥",
  "identity.verified": "身份检查通过",
  "password.changed": "修改密码",
  "password.reset": "重置密码",
  "session.login": "用户登录",
  "session.logout": "用户退出",
  "user.created": "创建用户",
  "user.deleted": "删除用户",
  "user.disabled": "禁用用户",
  "user.enabled": "启用用户",
};

function formatAuditAction(action: string): string {
  return AUDIT_ACTION_LABELS[action] || `其他操作（${action || "unknown"}）`;
}

const IDENTITY_ERROR_LABELS: Record<string, string> = {
  identity_validation_unavailable: "EVE 身份服务不可用",
  unauthorized_eve_character: "角色不在白名单",
};

function auditDetails(item: AuditRecord): Record<string, unknown> {
  return item.details && typeof item.details === "object" ? item.details : {};
}

function auditCharacterNames(details: Record<string, unknown>): string[] {
  if (!Array.isArray(details.characters)) return [];
  return details.characters
    .map((character) => {
      if (typeof character === "string") return character;
      if (!character || typeof character !== "object") return "";
      const value = character as Record<string, unknown>;
      return String(value.character_name || value.name || "");
    })
    .filter(Boolean);
}

function formatAuditDetails(item: AuditRecord): string {
  const details = auditDetails(item);
  const parts: string[] = [];
  const keyName = String(details.api_key_name || "");
  const keyPrefix = String(details.api_key_prefix || "");
  if (keyName || keyPrefix) {
    parts.push(`客户端 ${keyName || "未命名"}${keyPrefix ? `（${keyPrefix}）` : ""}`);
  } else if (details.api_key_id || details.key_id || details.name) {
    const name = String(details.name || "");
    const keyId = String(details.api_key_id || details.key_id || "");
    parts.push(`密钥 ${name || keyId.slice(0, 12)}`);
  }
  const characters = auditCharacterNames(details);
  if (details.character_name) characters.push(String(details.character_name));
  if (characters.length) parts.push(`角色 ${characters.join("、")}`);
  if (details.corporation_name) parts.push(`军团 ${String(details.corporation_name)}`);
  const errorCode = String(details.error_code || "");
  if (errorCode) {
    const friendly = IDENTITY_ERROR_LABELS[errorCode] || errorCode;
    const technical = String(details.reason || "").trim();
    parts.push(`原因 ${friendly}${technical && technical !== friendly ? `（${technical}）` : ""}`);
  }
  return parts.join(" · ");
}

type AuditCategory = "all" | "account" | "key" | "identity" | "policy" | "other";
type AuditPeriod = "all" | "24h" | "7d" | "30d";
type AuditOutcome = "all" | "normal" | "exception";

const AUDIT_EXCEPTION_ACTIONS = new Set([
  "identity.check_failed",
  "identity.user_disabled",
  "identity.key_revoked",
  "identity.desktop_keys_revoked",
]);

function auditActionCategory(action: string): AuditCategory {
  if (/^(user|password|session)\./.test(action)) return "account";
  if (action.startsWith("api_key.")) return "key";
  if (action.startsWith("identity.")) return "identity";
  if (/^(character|corporation)\./.test(action)) return "policy";
  return "other";
}

function isAuditException(item: AuditRecord): boolean {
  return AUDIT_EXCEPTION_ACTIONS.has(item.action);
}

function auditSearchText(item: AuditRecord): string {
  const details = auditDetails(item);
  return [
    item.action,
    formatAuditAction(item.action),
    item.actor_user_id,
    item.target_user_id,
    formatAuditDetails(item),
    JSON.stringify(details),
  ].join(" ").toLocaleLowerCase();
}

export function filterAuditRecords(
  records: AuditRecord[],
  filters: {
    search: string;
    category: AuditCategory;
    period: AuditPeriod;
    outcome: AuditOutcome;
  },
  now = Date.now(),
): AuditRecord[] {
  const query = filters.search.trim().toLocaleLowerCase();
  const periodMs = filters.period === "24h"
    ? 24 * 60 * 60 * 1000
    : filters.period === "7d"
      ? 7 * 24 * 60 * 60 * 1000
      : filters.period === "30d"
        ? 30 * 24 * 60 * 60 * 1000
        : 0;
  const threshold = periodMs ? now - periodMs : 0;
  return records.filter((item) => {
    if (query && !auditSearchText(item).includes(query)) return false;
    if (filters.category !== "all" && auditActionCategory(item.action) !== filters.category) return false;
    if (periodMs && new Date(item.created_at).getTime() < threshold) return false;
    const exception = isAuditException(item);
    if (filters.outcome === "exception" && !exception) return false;
    if (filters.outcome === "normal" && exception) return false;
    return true;
  });
}

function hasClientException(client: ClientHeartbeatRecord): boolean {
  return deriveClientHealth(client).isException;
}

function clientIdentity(client: ClientHeartbeatRecord): string {
  const details = client.details || {};
  const host = String(details.host || "未知主机");
  const version = String(details.client_version || "未知版本");
  return `${client.label || client.client_id}｜${host} · ${version}`;
}

function clientExceptionSummary(client: ClientHeartbeatRecord): string {
  const details = client.details || {};
  const health = deriveClientHealth(client);
  const targetError = Array.isArray(details.targets)
    ? details.targets
      .map((target) => target && typeof target === "object"
        ? String((target as Record<string, unknown>).last_error || "").trim()
        : "")
      .find(Boolean)
    : "";
  const lastError = String(details.last_error || "").trim()
    || targetError
    || (health.state === "offline_recent"
      ? "客户端心跳超时，当前离线"
      : health.state === "healthy" ? "运行正常" : "客户端状态异常");
  const lastAction = String(details.last_action || "").trim();
  return `${lastError}${lastAction ? `｜最后操作 ${lastAction}` : ""}`;
}

function clientHealthTag(client: ClientHeartbeatRecord) {
  const state = deriveClientHealth(client).state;
  if (state === "offline_recent") return <Tag color="orange">离线</Tag>;
  if (state === "warning") return <Tag color="red">异常</Tag>;
  return <Tag color="green">正常</Tag>;
}

const AUDIT_PAGE_SIZE = 20;

export function AdminAuditPage() {
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [clients, setClients] = useState<ClientHeartbeatRecord[]>([]);
  const [clientsLoadFailed, setClientsLoadFailed] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<AuditCategory>("all");
  const [period, setPeriod] = useState<AuditPeriod>("all");
  const [outcome, setOutcome] = useState<AuditOutcome>("all");
  const [page, setPage] = useState(1);

  const load = async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError("");
    const [auditResult, clientsResult] = await Promise.allSettled([
      listAudit(),
      fetchClients(),
    ]);
    const errors: string[] = [];
    if (auditResult.status === "fulfilled") {
      setAudit(auditResult.value);
    } else {
      errors.push(auditResult.reason instanceof Error
        ? auditResult.reason.message
        : "审计日志加载失败");
    }
    if (clientsResult.status === "fulfilled") {
      setClients(clientsResult.value.heartbeats || []);
      setClientsLoadFailed(false);
    } else {
      setClientsLoadFailed(true);
      errors.push(clientsResult.reason instanceof Error
        ? clientsResult.reason.message
        : "客户端状态加载失败");
    }
    setError(errors.join("；"));
    if (showLoading) setLoading(false);
  };
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(false); }, 15_000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => { setPage(1); }, [search, category, period, outcome]);

  const recentCount = useMemo(() => {
    const threshold = Date.now() - 24 * 60 * 60 * 1000;
    return audit.filter((item) => new Date(item.created_at).getTime() >= threshold).length;
  }, [audit]);
  const affectedUsers = new Set(audit.map((item) => item.target_user_id).filter(Boolean)).size;
  const exceptionClients = useMemo(
    () => clients.filter(hasClientException),
    [clients],
  );
  const visibleClients = useMemo(
    () => clients.filter((client) => deriveClientHealth(client).isRelevant),
    [clients],
  );
  const filteredAudit = useMemo(
    () => filterAuditRecords(audit, { category, outcome, period, search }),
    [audit, category, outcome, period, search],
  );
  const pagedAudit = filteredAudit.slice(
    (page - 1) * AUDIT_PAGE_SIZE,
    page * AUDIT_PAGE_SIZE,
  );
  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(filteredAudit.length / AUDIT_PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [filteredAudit.length]);

  return (
    <div className="admin-shell">
      <ManagementPageHeader loading={loading} refreshLabel="刷新日志" title="审计日志" onRefresh={() => void load()} />
      <ManagementError error={error} />
      <ManagementSummary ariaLabel="审计日志摘要" items={[
        { label: "已加载记录", value: audit.length },
        { label: "近 24 小时", value: recentCount },
        { label: "涉及用户", value: affectedUsers },
        { label: "当前异常客户端", value: exceptionClients.length },
      ]} />

      <section className="audit-page-grid">
        <Card className="account-panel audit-panel audit-page-panel arco-management-card" title={<Space><IconFile />操作记录</Space>}>
          <div className="audit-filter-bar">
            <Input.Search
              aria-label="搜索客户端、角色或用户"
              allowClear
              placeholder="搜索客户端、角色、用户或动作"
              value={search}
              onChange={setSearch}
            />
            <Select aria-label="操作类别" value={category} onChange={(value) => setCategory(value as AuditCategory)}>
              <Select.Option value="all">全部类别</Select.Option>
              <Select.Option value="account">账户</Select.Option>
              <Select.Option value="key">客户端密钥</Select.Option>
              <Select.Option value="identity">身份校验</Select.Option>
              <Select.Option value="policy">授权名单</Select.Option>
              <Select.Option value="other">其他</Select.Option>
            </Select>
            <Select aria-label="时间范围" value={period} onChange={(value) => setPeriod(value as AuditPeriod)}>
              <Select.Option value="all">全部时间</Select.Option>
              <Select.Option value="24h">近 24 小时</Select.Option>
              <Select.Option value="7d">近 7 天</Select.Option>
              <Select.Option value="30d">近 30 天</Select.Option>
            </Select>
            <Select aria-label="审计状态" value={outcome} onChange={(value) => setOutcome(value as AuditOutcome)}>
              <Select.Option value="all">全部状态</Select.Option>
              <Select.Option value="normal">正常操作</Select.Option>
              <Select.Option value="exception">异常与安全处置</Select.Option>
            </Select>
          </div>
          <Typography.Text className="audit-result-meta" type="secondary" aria-live="polite">
            筛选结果 {filteredAudit.length} 条 / 已加载 {audit.length} 条
          </Typography.Text>
          {pagedAudit.length ? (
            <List
              dataSource={pagedAudit}
              loading={loading}
              render={(item) => {
                const details = formatAuditDetails(item);
                return (
                  <List.Item
                    key={item.audit_id}
                    extra={<Space><Tag color={isAuditException(item) ? "red" : "green"}>{isAuditException(item) ? "异常/处置" : "正常"}</Tag><Tag icon={<IconUser />}>{item.actor_user_id || "系统"} → {item.target_user_id || "系统"}</Tag></Space>}
                  >
                    <List.Item.Meta
                      description={<time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</time>}
                      title={`${formatAuditAction(item.action)}${details ? `｜${details}` : ""}`}
                    />
                  </List.Item>
                );
              }}
            />
          ) : <Empty description={loading ? "正在加载审计记录" : "没有符合筛选条件的记录"} />}
          {filteredAudit.length > AUDIT_PAGE_SIZE ? (
            <Pagination
              className="audit-pagination"
              current={page}
              pageSize={AUDIT_PAGE_SIZE}
              total={filteredAudit.length}
              onChange={setPage}
            />
          ) : null}
        </Card>
        <Card
          className="account-panel audit-panel audit-page-panel arco-management-card"
          extra={<Space><Tag color={exceptionClients.length ? "red" : "green"}>{exceptionClients.length} 个异常</Tag><Typography.Text type="secondary">{visibleClients.length} 个已检查</Typography.Text></Space>}
          title={<Space><IconExclamationCircle />客户端异常检测</Space>}
        >
          {visibleClients.length ? (
            <List
              dataSource={visibleClients}
              render={(client) => (
                <List.Item
                  key={client.client_id}
                  extra={clientHealthTag(client)}
                >
                  <List.Item.Meta
                    description={<Space direction="vertical" size={2}><Typography.Text>{clientExceptionSummary(client)}</Typography.Text><Typography.Text type="secondary"><time dateTime={client.seen_at}>{new Date(client.seen_at).toLocaleString("zh-CN", { hour12: false })}</time></Typography.Text></Space>}
                    title={clientIdentity(client)}
                  />
                </List.Item>
              )}
            />
          ) : (
            <Empty description={clientsLoadFailed
              ? "客户端状态加载失败，暂时无法判断异常"
              : clients.length === 0
                ? "尚未收到任何客户端心跳，无法判断运行状态"
                : "当前只有主动停止或历史离线客户端"}
            />
          )}
        </Card>
      </section>
    </div>
  );
}
