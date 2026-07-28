import { useEffect, useMemo, useState } from "react";
import {
  BadgeCheck,
  Eye,
  Fingerprint,
  RefreshCw,
  Search,
  Users,
  X,
} from "lucide-react";

import { listAdminUsers } from "./api";
import type { AdminUser } from "./types";

export function AdminIdentityPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const nextUsers = await listAdminUsers();
      setUsers(nextUsers);
      setSelectedUserId((current) => nextUsers.some((user) => user.user_id === current) ? current : "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "身份策略加载失败");
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

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>身份记录</h2>
        </div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}><RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新数据</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="management-table-panel">
        <div className="management-table-toolbar">
          <div>
            <div className="account-panel-title"><Users size={17} /><h2>已验证身份</h2></div>
            <span>共 {filteredUsers.length} 个结果</span>
          </div>
          <label className="management-search-field">
            <Search size={14} />
            <input aria-label="搜索用户身份" placeholder="搜索用户名或显示名称" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
        </div>
        <div className="management-data-table identity-management-table">
          <div className="management-data-head identity-verification-row">
            <span>用户</span><span>已验证角色</span><span>账号状态</span><span>操作</span>
          </div>
          {filteredUsers.map((user) => (
            <div className="management-data-row identity-verification-row" key={user.user_id}>
              <div className="management-user-cell">
                <b>{(user.display_name || user.username).slice(0, 1).toUpperCase()}</b>
                <span><strong>{user.display_name || user.username}</strong><small>@{user.username}</small></span>
              </div>
              <span>{user.verified_characters.length}</span>
              <em className={`status-badge ${user.status}`}><BadgeCheck size={12} />{user.status === "active" ? "正常" : "已禁用"}</em>
              <button aria-label={`查看 ${user.display_name || user.username} 身份`} className="management-row-action" title="查看" type="button" onClick={() => setSelectedUserId(user.user_id)}><Eye size={15} />查看</button>
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
                <div><h2 id="identity-detail-title">{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username}</p></div>
              </div>
              <button aria-label="关闭身份详情" className="management-close-button" type="button" onClick={() => setSelectedUserId("")}><X size={17} /></button>
            </header>
            <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><em className={`status-badge ${selectedUser.status}`}><BadgeCheck size={13} />{selectedUser.status === "active" ? "正常" : "已禁用"}</em></div>
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
