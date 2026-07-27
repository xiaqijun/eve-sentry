import { useEffect, useMemo, useState } from "react";
import { RefreshCw, ScrollText, UserRound } from "lucide-react";

import { listAudit } from "./api";
import type { AuditRecord } from "./types";

const AUDIT_ACTION_LABELS: Record<string, string> = {
  "api_key.created": "创建密钥",
  "api_key.revoked": "吊销密钥",
  "character.whitelist_removed": "移除角色白名单",
  "character.whitelisted": "添加角色白名单",
  "corporation.allowed": "添加允许军团",
  "corporation.removed": "移除允许军团",
  "identity.user_disabled": "身份违规禁用用户",
  "identity.verified": "验证角色身份",
  "password.changed": "修改密码",
  "password.reset": "重置密码",
  "session.login": "用户登录",
  "session.logout": "用户退出",
  "user.created": "创建用户",
  "user.disabled": "禁用用户",
  "user.enabled": "启用用户",
};

function formatAuditAction(action: string): string {
  return AUDIT_ACTION_LABELS[action] || "其他操作";
}

export function AdminAuditPage() {
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      setAudit(await listAudit());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审计日志加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const recentCount = useMemo(() => {
    const threshold = Date.now() - 24 * 60 * 60 * 1000;
    return audit.filter((item) => new Date(item.created_at).getTime() >= threshold).length;
  }, [audit]);
  const affectedUsers = new Set(audit.map((item) => item.target_user_id).filter(Boolean)).size;

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>审计日志</h2>
        </div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}><RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新日志</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="admin-summary admin-summary-three" aria-label="审计日志摘要">
        <div><span>记录总数</span><strong>{audit.length}</strong></div>
        <div><span>近 24 小时</span><strong>{recentCount}</strong></div>
        <div><span>涉及用户</span><strong>{affectedUsers}</strong></div>
      </section>

      <section className="audit-page-grid">
        <article className="account-panel audit-panel audit-page-panel">
          <div className="account-panel-heading audit-page-heading">
            <div className="account-panel-title"><ScrollText size={17} /><h2>操作记录</h2></div>
          </div>
          <div className="audit-table-head"><span>时间</span><span>操作</span><span>目标用户</span></div>
          <div className="audit-list">
            {audit.slice(0, 100).map((item) => <div key={item.audit_id}><time>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</time><strong>{formatAuditAction(item.action)}</strong><span><UserRound size={13} />{item.target_user_id || "系统"}</span></div>)}
            {!loading && audit.length === 0 ? <p className="admin-empty">暂无审计记录</p> : null}
          </div>
        </article>
      </section>
    </div>
  );
}
