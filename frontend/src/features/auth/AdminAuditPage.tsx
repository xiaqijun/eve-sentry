import { useEffect, useMemo, useState } from "react";
import { RefreshCw, ScrollText, TriangleAlert, UserRound } from "lucide-react";

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
  return AUDIT_ACTION_LABELS[action] || "其他操作";
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
  } else if (details.api_key_id) {
    parts.push(`密钥 ${String(details.api_key_id).slice(0, 12)}`);
  }
  const characters = auditCharacterNames(details);
  if (characters.length) parts.push(`角色 ${characters.join("、")}`);
  const errorCode = String(details.error_code || "");
  if (errorCode) {
    parts.push(`原因 ${IDENTITY_ERROR_LABELS[errorCode] || String(details.reason || errorCode)}`);
  }
  return parts.join(" · ");
}

const CLIENT_ERROR_STATUSES = new Set(["error", "failed", "failure", "exception"]);

function hasClientException(client: ClientHeartbeatRecord): boolean {
  const lastError = String(client.details?.last_error || "").trim();
  const status = String(client.status || "").trim().toLowerCase();
  return client.online !== false && (Boolean(lastError) || CLIENT_ERROR_STATUSES.has(status));
}

function clientIdentity(client: ClientHeartbeatRecord): string {
  const details = client.details || {};
  const host = String(details.host || "未知主机");
  const version = String(details.client_version || "未知版本");
  return `${client.label || client.client_id}｜${host} · ${version}`;
}

function clientExceptionSummary(client: ClientHeartbeatRecord): string {
  const details = client.details || {};
  const lastError = String(details.last_error || "").trim() || "客户端状态异常";
  const lastAction = String(details.last_action || "").trim();
  return `${lastError}${lastAction ? `｜最后操作 ${lastAction}` : ""}`;
}

export function AdminAuditPage() {
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [clients, setClients] = useState<ClientHeartbeatRecord[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
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
    } else {
      setClients([]);
      errors.push(clientsResult.reason instanceof Error
        ? clientsResult.reason.message
        : "客户端状态加载失败");
    }
    setError(errors.join("；"));
    setLoading(false);
  };
  useEffect(() => { void load(); }, []);

  const recentCount = useMemo(() => {
    const threshold = Date.now() - 24 * 60 * 60 * 1000;
    return audit.filter((item) => new Date(item.created_at).getTime() >= threshold).length;
  }, [audit]);
  const affectedUsers = new Set(audit.map((item) => item.target_user_id).filter(Boolean)).size;
  const exceptionClients = useMemo(
    () => clients.filter(hasClientException),
    [clients],
  );

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>审计日志</h2>
        </div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}><RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新日志</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="admin-summary" aria-label="审计日志摘要">
        <div><span>记录总数</span><strong>{audit.length}</strong></div>
        <div><span>近 24 小时</span><strong>{recentCount}</strong></div>
        <div><span>涉及用户</span><strong>{affectedUsers}</strong></div>
        <div><span>当前异常客户端</span><strong>{exceptionClients.length}</strong></div>
      </section>

      <section className="audit-page-grid">
        <article className="account-panel audit-panel audit-page-panel">
          <div className="account-panel-heading audit-page-heading">
            <div className="account-panel-title"><ScrollText size={17} /><h2>操作记录</h2></div>
          </div>
          <div className="audit-table-head"><span>时间</span><span>检测结果</span><span>用户</span></div>
          <div className="audit-list">
            {audit.slice(0, 100).map((item) => {
              const details = formatAuditDetails(item);
              return <div key={item.audit_id}><time>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</time><strong>{formatAuditAction(item.action)}{details ? `｜${details}` : ""}</strong><span><UserRound size={13} />{item.target_user_id || "系统"}</span></div>;
            })}
            {!loading && audit.length === 0 ? <p className="admin-empty">暂无审计记录</p> : null}
          </div>
        </article>
        <article className="account-panel audit-panel audit-page-panel">
          <div className="account-panel-heading audit-page-heading">
            <div className="account-panel-title"><TriangleAlert size={17} /><h2>客户端异常检测</h2></div>
            <span>{exceptionClients.length} 个异常</span>
          </div>
          <div className="audit-table-head"><span>时间</span><span>客户端</span><span>异常</span></div>
          <div className="audit-list">
            {exceptionClients.map((client) => <div key={client.client_id}><time>{new Date(client.seen_at).toLocaleString("zh-CN", { hour12: false })}</time><strong>{clientIdentity(client)}</strong><span>{clientExceptionSummary(client)}</span></div>)}
            {!loading && exceptionClients.length === 0 ? <p className="admin-empty">暂无客户端异常</p> : null}
          </div>
        </article>
      </section>
    </div>
  );
}
