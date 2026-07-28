import { FormEvent, useEffect, useMemo, useState } from "react";
import { Building2, Eye, Plus, RefreshCw, Search, Trash2, Users, X } from "lucide-react";

import {
  addCorporation,
  addWhitelistCharacter,
  listAdminUsers,
  listCorporations,
  removeCorporation,
  removeWhitelistCharacter,
} from "./api";
import type { AdminUser, AllowedCorporation } from "./types";

export function AdminWhitelistPage() {
  const [corporations, setCorporations] = useState<AllowedCorporation[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
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
      const [nextCorporations, nextUsers] = await Promise.all([listCorporations(), listAdminUsers()]);
      setCorporations(nextCorporations);
      setUsers(nextUsers);
      setSelectedUserId((current) => nextUsers.some((user) => user.user_id === current) ? current : "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "白名单加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const selectedUser = users.find((item) => item.user_id === selectedUserId);
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

  const submitCorporation = (event: FormEvent) => {
    event.preventDefault();
    void run(() => addCorporation(Number(corporationId)));
    setCorporationId("");
  };

  const submitCharacter = (event: FormEvent) => {
    event.preventDefault();
    if (!selectedUser) return;
    void run(() => addWhitelistCharacter(selectedUser.user_id, Number(characterId), characterNote));
    setCharacterId("");
    setCharacterNote("");
  };

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div><h2>白名单管理</h2></div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}>
          <RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新
        </button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="management-table-panel">
        <div className="management-table-toolbar">
          <div className="account-panel-title"><Building2 size={17} /><h2>军团白名单</h2></div>
          <form className="management-policy-form" onSubmit={submitCorporation}>
            <input aria-label="军团 ID" inputMode="numeric" placeholder="军团 ID" required value={corporationId} onChange={(event) => setCorporationId(event.target.value)} />
            <button className="management-primary-button" type="submit"><Plus size={15} />添加</button>
          </form>
        </div>
        <div className="identity-corporation-list">
          {corporations.map((corporation) => (
            <div key={corporation.corporation_id}>
              <span className="identity-corporation-icon"><Building2 size={15} /></span>
              <span><strong>{corporation.corporation_name || "未知军团"}</strong><small>ID {corporation.corporation_id}</small></span>
              <button aria-label={`移除 ${corporation.corporation_name || corporation.corporation_id}`} className="icon-button" title="移除" type="button" onClick={() => void run(() => removeCorporation(corporation.corporation_id))}><Trash2 size={14} /></button>
            </div>
          ))}
          {!loading && corporations.length === 0 ? <p className="management-table-empty">暂无军团</p> : null}
        </div>
      </section>

      <section className="management-table-panel">
        <div className="management-table-toolbar">
          <div>
            <div className="account-panel-title"><Users size={17} /><h2>角色白名单</h2></div>
            <span>共 {filteredUsers.length} 个用户</span>
          </div>
          <label className="management-search-field">
            <Search size={14} />
            <input aria-label="搜索用户" placeholder="搜索用户" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
        </div>
        <div className="management-data-table">
          <div className="management-data-head whitelist-user-row">
            <span>用户</span><span>白名单角色</span><span>操作</span>
          </div>
          {filteredUsers.map((user) => (
            <div className="management-data-row whitelist-user-row" key={user.user_id}>
              <div className="management-user-cell">
                <b>{(user.display_name || user.username).slice(0, 1).toUpperCase()}</b>
                <span><strong>{user.display_name || user.username}</strong><small>@{user.username}</small></span>
              </div>
              <span>{user.whitelist.length}</span>
              <button aria-label={`管理 ${user.display_name || user.username} 白名单`} className="management-row-action" title="管理" type="button" onClick={() => setSelectedUserId(user.user_id)}><Eye size={15} />管理</button>
            </div>
          ))}
          {!loading && filteredUsers.length === 0 ? <div className="management-table-empty">暂无用户</div> : null}
        </div>
      </section>

      {selectedUser ? (
        <div className="management-drawer-backdrop" role="presentation" onMouseDown={() => setSelectedUserId("")}>
          <aside aria-labelledby="whitelist-detail-title" aria-modal="true" className="management-drawer" role="dialog" onMouseDown={(event) => event.stopPropagation()}>
            <header className="management-drawer-header">
              <div className="admin-user-heading">
                <span className="admin-user-avatar"><Users size={19} /></span>
                <div><h2 id="whitelist-detail-title">{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username}</p></div>
              </div>
              <button aria-label="关闭白名单详情" className="management-close-button" type="button" onClick={() => setSelectedUserId("")}><X size={17} /></button>
            </header>
            <div className="management-drawer-body">
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>角色白名单</h3><span>{selectedUser.whitelist.length} 个</span></div>
                <form className="identity-character-form" onSubmit={submitCharacter}>
                  <input aria-label="角色 ID" inputMode="numeric" placeholder="角色 ID" required value={characterId} onChange={(event) => setCharacterId(event.target.value)} />
                  <input aria-label="备注" placeholder="备注（可选）" value={characterNote} onChange={(event) => setCharacterNote(event.target.value)} />
                  <button className="management-primary-button" type="submit"><Plus size={14} />添加</button>
                </form>
                <div className="compact-list">
                  {selectedUser.whitelist.map((item) => <div key={item.character_id}><span><strong>{item.character_name}</strong><small>ID {item.character_id}{item.note ? ` · ${item.note}` : ""}</small></span><button aria-label={`移除 ${item.character_name}`} className="icon-button" title="移除" type="button" onClick={() => void run(() => removeWhitelistCharacter(selectedUser.user_id, item.character_id))}><Trash2 size={14} /></button></div>)}
                  {selectedUser.whitelist.length === 0 ? <p className="admin-empty">暂无角色</p> : null}
                </div>
              </section>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
