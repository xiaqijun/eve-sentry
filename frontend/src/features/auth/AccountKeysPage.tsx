import { FormEvent, useEffect, useState } from "react";
import { BadgeCheck, KeyRound, Plus, Trash2 } from "lucide-react";

import { createMyKey, listMyKeys, revokeKey } from "./api";
import { useAuth } from "./AuthContext";
import type { ApiKeyRecord } from "./types";

function formatTime(value?: string): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "从未";
}

export function AccountKeysPage() {
  const { user } = useAuth();
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdSecret, setCreatedSecret] = useState("");
  const [error, setError] = useState("");
  const activeKeys = keys.filter((key) => key.status === "active");
  const verifiedKeys = activeKeys.filter((key) => key.identity_verified);

  const loadKeys = async () => setKeys(await listMyKeys());
  useEffect(() => { void loadKeys().catch((reason) => setError(String(reason))); }, []);

  const createKey = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const key = await createMyKey(newKeyName || "监控客户端");
      setCreatedSecret(key.secret || "");
      setNewKeyName("");
      await loadKeys();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密钥创建失败");
    }
  };

  return (
    <div className="account-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>设备密钥</h2>
        </div>
        <span className="page-identity-badge"><BadgeCheck size={16} />{user?.display_name || user?.username}</span>
      </header>

      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="account-summary" aria-label="密钥摘要">
        <div><span>有效密钥</span><strong>{activeKeys.length}</strong></div>
        <div><span>身份已校验</span><strong>{verifiedKeys.length}</strong></div>
        <div><span>等待校验</span><strong>{activeKeys.length - verifiedKeys.length}</strong></div>
      </section>

      <section className="account-grid account-grid-single">
        <article className="account-panel">
          <div className="account-panel-heading">
            <div className="account-panel-title"><KeyRound size={17} /><h2>客户端访问凭据</h2></div>
          </div>
          <form className="inline-form account-key-form" onSubmit={createKey}>
            <input maxLength={80} placeholder="设备名称" value={newKeyName} onChange={(event) => setNewKeyName(event.target.value)} />
            <button type="submit"><Plus size={15} />创建设备密钥</button>
          </form>
          {createdSecret ? (
            <div className="secret-once" role="status">
              <strong>密钥只显示这一次</strong>
              <code>{createdSecret}</code>
              <button type="button" onClick={() => void navigator.clipboard.writeText(createdSecret)}>复制密钥</button>
            </div>
          ) : null}
          <div className="management-data-table device-key-table">
            <div className="management-data-head device-key-row">
              <span>设备名称</span><span>密钥前缀</span><span>身份校验</span><span>状态</span><span>最后使用</span><span>操作</span>
            </div>
            {keys.map((key) => (
              <div className="management-data-row device-key-row" key={key.key_id}>
                <div className="management-user-cell device-key-name"><b><KeyRound size={14} /></b><span><strong>{key.name}</strong><small>{key.key_type === "service_readonly" ? "只读服务" : "监控客户端"}</small></span></div>
                <code>{key.key_prefix}…</code>
                <span>{key.identity_verified ? "已校验" : "等待校验"}</span>
                <em className={`status-badge ${key.status}`}>{key.status === "active" ? "有效" : "已吊销"}</em>
                <time>{formatTime(key.last_used_at)}</time>
                {key.status === "active" ? (
                  <button aria-label={`吊销 ${key.name}`} className="management-row-action" title="吊销密钥" type="button" onClick={() => void revokeKey(key.key_id).then(loadKeys)}>
                    <Trash2 size={14} />吊销
                  </button>
                ) : <span>-</span>}
              </div>
            ))}
            {keys.length === 0 ? <p className="management-table-empty">尚未创建设备密钥</p> : null}
          </div>
        </article>
      </section>
    </div>
  );
}
