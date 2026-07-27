import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  BadgeCheck,
  Building2,
  Eye,
  Fingerprint,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Users,
  X,
} from "lucide-react";

import {
  addCorporation,
  addWhitelistCharacter,
  listAdminUsers,
  listCorporations,
  removeCorporation,
  removeWhitelistCharacter,
} from "./api";
import type { AdminUser, AllowedCorporation } from "./types";

export function AdminIdentityPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [corporations, setCorporations] = useState<AllowedCorporation[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [search, setSearch] = useState("");
  const [corporationId, setCorporationId] = useState("");
  const [characterId, setCharacterId] = useState("");
  const [characterNote, setCharacterNote] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [nextUsers, nextCorporations] = await Promise.all([listAdminUsers(), listCorporations()]);
      setUsers(nextUsers);
      setCorporations(nextCorporations);
      setSelectedUserId((current) => nextUsers.some((user) => user.user_id === current) ? current : "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "身份策略加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const selectedUser = users.find((item) => item.user_id === selectedUserId);
  const whitelistCount = users.reduce((total, user) => total + user.whitelist.length, 0);
  const verifiedCount = users.reduce((total, user) => total + user.verified_characters.length, 0);
  const filteredUsers = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return users;
    return users.filter((user) => [user.username, user.display_name]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  }, [search, users]);

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    }
  };

  const addCharacter = (event: FormEvent) => {
    event.preventDefault();
    if (!selectedUser) return;
    void run(() => addWhitelistCharacter(selectedUser.user_id, Number(characterId), characterNote));
    setCharacterId("");
    setCharacterNote("");
  };

  const submitCorporation = (event: FormEvent) => {
    event.preventDefault();
    void run(() => addCorporation(Number(corporationId)));
    setCorporationId("");
  };

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>身份授权</h2>
        </div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}><RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新数据</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="admin-summary" aria-label="身份授权摘要">
        <div><span>授权用户</span><strong>{users.length}</strong></div>
        <div><span>允许军团</span><strong>{corporations.length}</strong></div>
        <div><span>白名单角色</span><strong>{whitelistCount}</strong></div>
        <div><span>已验证角色</span><strong>{verifiedCount}</strong></div>
      </section>

      <section className="management-table-panel identity-corporation-panel">
        <div className="management-table-toolbar">
          <div>
            <div className="account-panel-title"><Building2 size={17} /><h2>允许军团</h2></div>
          </div>
          <form className="management-policy-form" onSubmit={submitCorporation}>
            <input inputMode="numeric" placeholder="输入军团 ID" required value={corporationId} onChange={(event) => setCorporationId(event.target.value)} />
            <button className="management-primary-button" type="submit"><Plus size={15} />添加军团</button>
          </form>
        </div>
        <div className="identity-corporation-list">
          {corporations.map((corporation) => (
            <div key={corporation.corporation_id}>
              <span className="identity-corporation-icon"><Building2 size={15} /></span>
              <span><strong>{corporation.corporation_name || "未知军团"}</strong><small>ID {corporation.corporation_id}</small></span>
              <button aria-label={`移除 ${corporation.corporation_name || corporation.corporation_id}`} className="icon-button" title="移除军团" type="button" onClick={() => void run(() => removeCorporation(corporation.corporation_id))}><Trash2 size={14} /></button>
            </div>
          ))}
          {!loading && corporations.length === 0 ? <p className="management-table-empty">尚未配置允许军团</p> : null}
        </div>
      </section>

      <section className="management-table-panel">
        <div className="management-table-toolbar">
          <div>
            <div className="account-panel-title"><Users size={17} /><h2>用户身份列表</h2></div>
            <span>共 {filteredUsers.length} 个结果</span>
          </div>
          <label className="management-search-field">
            <Search size={14} />
            <input aria-label="搜索用户身份" placeholder="搜索用户名或显示名称" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
        </div>
        <div className="management-data-table identity-management-table">
          <div className="management-data-head identity-management-row">
            <span>用户</span><span>角色白名单</span><span>已验证角色</span><span>账号状态</span><span>操作</span>
          </div>
          {filteredUsers.map((user) => (
            <div className="management-data-row identity-management-row" key={user.user_id}>
              <div className="management-user-cell">
                <b>{(user.display_name || user.username).slice(0, 1).toUpperCase()}</b>
                <span><strong>{user.display_name || user.username}</strong><small>@{user.username}</small></span>
              </div>
              <span>{user.whitelist.length}</span>
              <span>{user.verified_characters.length}</span>
              <em className={`status-badge ${user.status}`}><BadgeCheck size={12} />{user.status === "active" ? "正常" : "已禁用"}</em>
              <button aria-label={`管理 ${user.display_name || user.username} 身份`} className="management-row-action" title="管理身份" type="button" onClick={() => setSelectedUserId(user.user_id)}><Eye size={15} />管理</button>
            </div>
          ))}
          {!loading && filteredUsers.length === 0 ? <div className="management-table-empty">没有符合条件的用户</div> : null}
        </div>
      </section>

      {selectedUser ? (
        <div className="management-drawer-backdrop" role="presentation" onMouseDown={() => setSelectedUserId("")}>
          <aside aria-labelledby="identity-detail-title" aria-modal="true" className="management-drawer" role="dialog" onMouseDown={(event) => event.stopPropagation()}>
            <header className="management-drawer-header">
              <div className="admin-user-heading">
                <span className="admin-user-avatar"><Fingerprint size={19} /></span>
                <div><span className="admin-user-heading-meta">身份策略</span><h2 id="identity-detail-title">{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username} · 独立角色白名单</p></div>
              </div>
              <button aria-label="关闭身份详情" className="management-close-button" type="button" onClick={() => setSelectedUserId("")}><X size={17} /></button>
            </header>
            <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><em className={`status-badge ${selectedUser.status}`}><BadgeCheck size={13} />{selectedUser.status === "active" ? "正常" : "已禁用"}</em></div>
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>角色白名单</h3><span>{selectedUser.whitelist.length} 个</span></div>
                <form className="identity-character-form" onSubmit={addCharacter}>
                  <input inputMode="numeric" placeholder="角色 ID" required value={characterId} onChange={(event) => setCharacterId(event.target.value)} />
                  <input placeholder="备注（可选）" value={characterNote} onChange={(event) => setCharacterNote(event.target.value)} />
                  <button className="management-primary-button" type="submit"><Plus size={14} />添加</button>
                </form>
                <div className="compact-list">
                  {selectedUser.whitelist.map((item) => <div key={item.character_id}><span><strong>{item.character_name}</strong><small>ID {item.character_id}{item.note ? ` · ${item.note}` : ""}</small></span><button className="icon-button" title="移除白名单" type="button" onClick={() => void run(() => removeWhitelistCharacter(selectedUser.user_id, item.character_id))}><Trash2 size={14} /></button></div>)}
                  {selectedUser.whitelist.length === 0 ? <p className="admin-empty">暂无白名单角色</p> : null}
                </div>
              </section>
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>已验证角色</h3><span>{selectedUser.verified_characters.length} 个</span></div>
                <div className="compact-list">
                  {selectedUser.verified_characters.map((item) => <div key={item.character_id}><span><strong>{item.character_name}</strong><small>{item.corporation_name || item.corporation_id || "未知军团"}</small></span></div>)}
                  {selectedUser.verified_characters.length === 0 ? <p className="admin-empty">尚未发现已验证角色</p> : null}
                </div>
              </section>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
