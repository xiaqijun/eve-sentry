import { FormEvent, useEffect, useState } from "react";
import { KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";

import {
  changePassword,
  createMyKey,
  listMyKeys,
  revokeKey,
} from "./api";
import { useAuth } from "./AuthContext";
import type { ApiKeyRecord } from "./types";

function formatTime(value?: string): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "从未";
}

export function AccountPage() {
  const { refresh, user } = useAuth();
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdSecret, setCreatedSecret] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

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

  const updatePassword = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setMessage("密码已更新");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密码更新失败");
    }
  };

  return (
    <div className="account-shell">
      <header className="content-page-header account-header">
        <div>
          <p className="content-page-kicker">个人设置</p>
          <h2>{user?.display_name || user?.username}</h2>
          <span>{user?.role === "admin" ? "管理员" : "普通用户"} · {user?.username}</span>
        </div>
      </header>

      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}
      {message ? <div className="auth-banner">{message}</div> : null}

      <section className="account-grid">
        <article className="account-panel">
          <div className="account-panel-title"><KeyRound size={17} /><h2>客户端密钥</h2></div>
          <form className="inline-form" onSubmit={createKey}>
            <input maxLength={80} placeholder="设备名称" value={newKeyName} onChange={(e) => setNewKeyName(e.target.value)} />
            <button type="submit"><Plus size={15} />创建设备密钥</button>
          </form>
          {createdSecret ? (
            <div className="secret-once" role="status">
              <strong>密钥只显示这一次</strong>
              <code>{createdSecret}</code>
              <button type="button" onClick={() => void navigator.clipboard.writeText(createdSecret)}>复制</button>
            </div>
          ) : null}
          <div className="key-list">
            {keys.map((key) => (
              <div className="key-row" key={key.key_id}>
                <div>
                  <strong>{key.name}</strong>
                  <span>{key.key_prefix}… · {key.identity_verified ? "身份已校验" : "等待身份校验"}</span>
                  <small>最后使用：{formatTime(key.last_used_at)}</small>
                </div>
                <em className={key.status}>{key.status === "active" ? "有效" : "已吊销"}</em>
                {key.status === "active" ? (
                  <button aria-label={`吊销 ${key.name}`} className="icon-button" title="吊销密钥" type="button" onClick={() => void revokeKey(key.key_id).then(loadKeys)}>
                    <Trash2 size={15} />
                  </button>
                ) : null}
              </div>
            ))}
          </div>
        </article>

        <article className="account-panel">
          <div className="account-panel-title"><ShieldCheck size={17} /><h2>修改密码</h2></div>
          <form className="stack-form" onSubmit={updatePassword}>
            <label><span>当前密码</span><input autoComplete="current-password" required type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} /></label>
            <label><span>新密码</span><input autoComplete="new-password" minLength={12} required type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} /></label>
            <button type="submit">更新密码</button>
          </form>
        </article>
      </section>
    </div>
  );
}
