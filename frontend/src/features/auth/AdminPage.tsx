import { FormEvent, useEffect, useState } from "react";
import { ArrowLeft, Building2, KeyRound, Plus, RefreshCw, ShieldAlert, Trash2, UserPlus, Users } from "lucide-react";
import { Link } from "react-router-dom";

import {
  addCorporation,
  addWhitelistCharacter,
  createServiceKey,
  createUser,
  listAdminUsers,
  listAudit,
  listCorporations,
  removeCorporation,
  removeWhitelistCharacter,
  resetUserPassword,
  revokeKey,
  setUserActive,
} from "./api";
import type { AdminUser, AllowedCorporation, ApiKeyRecord, AuditRecord } from "./types";

export function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [corporations, setCorporations] = useState<AllowedCorporation[]>([]);
  const [audit, setAudit] = useState<AuditRecord[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [corporationId, setCorporationId] = useState("");
  const [characterId, setCharacterId] = useState("");
  const [characterNote, setCharacterNote] = useState("");
  const [serviceSecret, setServiceSecret] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [nextUsers, nextCorporations, nextAudit] = await Promise.all([
        listAdminUsers(), listCorporations(), listAudit(),
      ]);
      setUsers(nextUsers);
      setCorporations(nextCorporations);
      setAudit(nextAudit);
      setSelectedUserId((current) => current || nextUsers[0]?.user_id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "管理数据加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);
  const selectedUser = users.find((item) => item.user_id === selectedUserId);

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try { await action(); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
  };

  const submitUser = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    void run(() => createUser({
      username: String(data.get("username") || ""),
      display_name: String(data.get("display_name") || ""),
      password: String(data.get("password") || ""),
      role: data.get("role") === "admin" ? "admin" : "member",
    })).then(() => form.reset());
  };

  const createReadonlyKey = async () => {
    if (!selectedUserId) return;
    try {
      const key: ApiKeyRecord = await createServiceKey(selectedUserId, "QQ 机器人");
      setServiceSecret(key.secret || "");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "服务密钥创建失败");
    }
  };

  return (
    <main className="admin-shell">
      <header className="account-header">
        <div>
          <Link to="/"><ArrowLeft size={16} />返回态势图</Link>
          <p className="eyebrow">访问控制</p><h1>用户与 EVE 身份管理</h1>
        </div>
        <button disabled={loading} type="button" onClick={() => void load()}><RefreshCw size={15} />刷新</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="admin-grid">
        <article className="account-panel admin-users-panel">
          <div className="account-panel-title"><Users size={17} /><h2>用户</h2></div>
          <form className="admin-create-user" onSubmit={submitUser}>
            <input name="username" placeholder="用户名" required />
            <input name="display_name" placeholder="显示名称" />
            <input minLength={12} name="password" placeholder="初始密码（至少 12 位）" required type="password" />
            <select defaultValue="member" name="role"><option value="member">普通用户</option><option value="admin">管理员</option></select>
            <button type="submit"><UserPlus size={15} />创建</button>
          </form>
          <div className="admin-user-list">
            {users.map((user) => (
              <button className={selectedUserId === user.user_id ? "active" : ""} key={user.user_id} type="button" onClick={() => { setSelectedUserId(user.user_id); setServiceSecret(""); }}>
                <span><strong>{user.display_name || user.username}</strong><small>{user.username} · {user.role}</small></span>
                <em className={user.status}>{user.status === "active" ? "有效" : "禁用"}</em>
              </button>
            ))}
          </div>
        </article>

        <article className="account-panel admin-detail-panel">
          <div className="account-panel-title"><ShieldAlert size={17} /><h2>{selectedUser?.display_name || "选择用户"}</h2></div>
          {selectedUser ? (
            <>
              <div className="admin-actions-row">
                <button type="button" onClick={() => void run(() => setUserActive(selectedUser.user_id, selectedUser.status !== "active", "管理员操作"))}>
                  {selectedUser.status === "active" ? "禁用用户" : "解禁用户"}
                </button>
                <button type="button" onClick={() => {
                  const password = window.prompt("输入至少 12 位的新密码");
                  if (password) void run(() => resetUserPassword(selectedUser.user_id, password));
                }}>重置密码</button>
                <button type="button" onClick={() => void createReadonlyKey()}><KeyRound size={14} />创建只读服务密钥</button>
              </div>
              {serviceSecret ? <div className="secret-once"><strong>服务密钥只显示这一次</strong><code>{serviceSecret}</code></div> : null}
              <h3>设备与服务密钥</h3>
              <div className="compact-list">
                {selectedUser.keys.map((key) => <div key={key.key_id}><span>{key.name} · {key.key_prefix}… · {key.key_type}</span><em>{key.status}</em>{key.status === "active" ? <button className="icon-button" title="吊销密钥" type="button" onClick={() => void run(() => revokeKey(key.key_id))}><Trash2 size={14} /></button> : null}</div>)}
              </div>
              <h3>角色白名单</h3>
              <form className="inline-form" onSubmit={(event) => { event.preventDefault(); void run(() => addWhitelistCharacter(selectedUser.user_id, Number(characterId), characterNote)); setCharacterId(""); setCharacterNote(""); }}>
                <input inputMode="numeric" placeholder="角色 ID" required value={characterId} onChange={(e) => setCharacterId(e.target.value)} />
                <input placeholder="备注" value={characterNote} onChange={(e) => setCharacterNote(e.target.value)} />
                <button type="submit"><Plus size={14} />添加</button>
              </form>
              <div className="compact-list">
                {selectedUser.whitelist.map((item) => <div key={item.character_id}><span>{item.character_name} · {item.character_id}</span><small>{item.note}</small><button className="icon-button" title="移除白名单" type="button" onClick={() => void run(() => removeWhitelistCharacter(selectedUser.user_id, item.character_id))}><Trash2 size={14} /></button></div>)}
              </div>
              <h3>已验证角色</h3>
              <div className="compact-list">{selectedUser.verified_characters.map((item) => <div key={item.character_id}><span>{item.character_name}</span><small>{item.corporation_name || item.corporation_id || "未知军团"}</small></div>)}</div>
            </>
          ) : null}
        </article>

        <article className="account-panel">
          <div className="account-panel-title"><Building2 size={17} /><h2>允许军团</h2></div>
          <form className="inline-form" onSubmit={(event) => { event.preventDefault(); void run(() => addCorporation(Number(corporationId))); setCorporationId(""); }}>
            <input inputMode="numeric" placeholder="军团 ID" required value={corporationId} onChange={(e) => setCorporationId(e.target.value)} />
            <button type="submit"><Plus size={14} />添加</button>
          </form>
          <div className="compact-list">{corporations.map((corp) => <div key={corp.corporation_id}><span>{corp.corporation_name || "未知军团"}</span><small>{corp.corporation_id}</small><button className="icon-button" title="移除军团" type="button" onClick={() => void run(() => removeCorporation(corp.corporation_id))}><Trash2 size={14} /></button></div>)}</div>
        </article>

        <article className="account-panel audit-panel">
          <div className="account-panel-title"><ShieldAlert size={17} /><h2>审计记录</h2></div>
          <div className="audit-list">{audit.slice(0, 100).map((item) => <div key={item.audit_id}><time>{new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })}</time><strong>{item.action}</strong><span>{item.target_user_id || "系统"}</span></div>)}</div>
        </article>
      </section>
    </main>
  );
}
