import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  BadgeCheck,
  Eye,
  KeyRound,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import {
  createServiceKey,
  createUser,
  listAdminUsers,
  resetUserPassword,
  revokeKey,
  setUserActive,
} from "./api";
import type { AdminUser, ApiKeyRecord } from "./types";

type UserStatusFilter = "all" | "active" | "disabled";

export function AdminUsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<UserStatusFilter>("all");
  const [createRole, setCreateRole] = useState<"admin" | "member">("member");
  const [createOpen, setCreateOpen] = useState(false);
  const [serviceSecret, setServiceSecret] = useState("");
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
      setError(reason instanceof Error ? reason.message : "用户数据加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void load(); }, []);

  const selectedUser = users.find((item) => item.user_id === selectedUserId);
  const activeUsers = users.filter((item) => item.status === "active").length;
  const activeKeys = users.reduce(
    (total, item) => total + item.keys.filter((key) => key.status === "active").length,
    0,
  );
  const filteredUsers = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return users.filter((user) => {
      if (statusFilter !== "all" && user.status !== statusFilter) return false;
      if (!query) return true;
      return [user.username, user.display_name, user.role]
        .some((value) => String(value || "").toLocaleLowerCase().includes(query));
    });
  }, [search, statusFilter, users]);

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    }
  };

  const submitUser = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    void run(() => createUser({
      username: String(data.get("username") || ""),
      display_name: String(data.get("display_name") || ""),
      password: String(data.get("password") || ""),
      role: createRole,
    })).then(() => {
      form.reset();
      setCreateRole("member");
      setCreateOpen(false);
    });
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

  const openUser = (userId: string) => {
    setSelectedUserId(userId);
    setServiceSecret("");
  };

  return (
    <div className="admin-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>用户管理</h2>
        </div>
        <button className="page-refresh-button" disabled={loading} type="button" onClick={() => void load()}><RefreshCw className={loading ? "is-spinning" : ""} size={15} />刷新数据</button>
      </header>
      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}

      <section className="admin-summary admin-summary-three" aria-label="用户管理摘要">
        <div><span>平台用户</span><strong>{users.length}</strong></div>
        <div><span>正常用户</span><strong>{activeUsers}</strong></div>
        <div><span>有效密钥</span><strong>{activeKeys}</strong></div>
      </section>

      <section className="management-table-panel">
        <div className="management-table-toolbar">
          <div>
            <div className="account-panel-title"><Users size={17} /><h2>用户列表</h2></div>
            <span>共 {filteredUsers.length} 个结果</span>
          </div>
          <div className="management-table-actions">
            <label className="management-search-field">
              <Search size={14} />
              <input aria-label="搜索用户" placeholder="搜索用户名或显示名称" value={search} onChange={(event) => setSearch(event.target.value)} />
            </label>
            <select aria-label="筛选用户状态" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as UserStatusFilter)}>
              <option value="all">全部状态</option>
              <option value="active">正常</option>
              <option value="disabled">已禁用</option>
            </select>
            <button className="management-primary-button" type="button" onClick={() => setCreateOpen(true)}><Plus size={15} />新建用户</button>
          </div>
        </div>

        <div className="management-data-table user-management-table">
          <div className="management-data-head user-management-row">
            <span>用户</span><span>角色</span><span>状态</span><span>有效密钥</span><span>已验证角色</span><span>操作</span>
          </div>
          {filteredUsers.map((user) => (
            <div className="management-data-row user-management-row" key={user.user_id}>
              <div className="management-user-cell">
                <b>{(user.display_name || user.username).slice(0, 1).toUpperCase()}</b>
                <span><strong>{user.display_name || user.username}</strong><small>@{user.username}</small></span>
              </div>
              <span>{user.role === "admin" ? "管理员" : "普通用户"}</span>
              <em className={`status-badge ${user.status}`}><BadgeCheck size={12} />{user.status === "active" ? "正常" : "已禁用"}</em>
              <span>{user.keys.filter((key) => key.status === "active").length}</span>
              <span>{user.verified_characters.length}</span>
              <button aria-label={`查看 ${user.display_name || user.username}`} className="management-row-action" title="查看详情" type="button" onClick={() => openUser(user.user_id)}><Eye size={15} />查看</button>
            </div>
          ))}
          {!loading && filteredUsers.length === 0 ? <div className="management-table-empty">没有符合条件的用户</div> : null}
        </div>
      </section>

      {createOpen ? (
        <div className="management-modal-backdrop" role="presentation" onMouseDown={() => setCreateOpen(false)}>
          <section aria-labelledby="create-user-title" aria-modal="true" className="management-modal" role="dialog" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span>新建账号</span><h2 id="create-user-title">创建平台用户</h2></div><button aria-label="关闭" className="management-close-button" type="button" onClick={() => setCreateOpen(false)}><X size={17} /></button></header>
            <form className="management-dialog-form" onSubmit={submitUser}>
              <label><span>用户名</span><input name="username" placeholder="用于登录" required /></label>
              <label><span>显示名称</span><input name="display_name" placeholder="页面展示名称" /></label>
              <label><span>权限角色</span><select name="role" value={createRole} onChange={(event) => setCreateRole(event.target.value === "admin" ? "admin" : "member")}><option value="member">普通用户</option><option value="admin">管理员</option></select></label>
              {createRole === "admin" ? <label><span>初始密码</span><input minLength={12} name="password" placeholder="至少 12 位" required type="password" /></label> : null}
              <footer><button type="button" onClick={() => setCreateOpen(false)}>取消</button><button className="management-primary-button" type="submit"><UserPlus size={15} />创建用户</button></footer>
            </form>
          </section>
        </div>
      ) : null}

      {selectedUser ? (
        <div className="management-drawer-backdrop" role="presentation" onMouseDown={() => setSelectedUserId("")}>
          <aside aria-labelledby="user-detail-title" aria-modal="true" className="management-drawer" role="dialog" onMouseDown={(event) => event.stopPropagation()}>
            <header className="management-drawer-header">
              <div className="admin-user-heading">
                <span className="admin-user-avatar">{(selectedUser.display_name || selectedUser.username).slice(0, 1).toUpperCase()}</span>
                <div><span className="admin-user-heading-meta">用户详情</span><h2 id="user-detail-title">{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username} · {selectedUser.role === "admin" ? "管理员" : "普通用户"}</p></div>
              </div>
              <button aria-label="关闭详情" className="management-close-button" type="button" onClick={() => setSelectedUserId("")}><X size={17} /></button>
            </header>
            <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><em className={`status-badge ${selectedUser.status}`}><BadgeCheck size={13} />{selectedUser.status === "active" ? "正常" : "已禁用"}</em></div>
              <div className="admin-actions-row">
                <button type="button" onClick={() => void run(() => setUserActive(selectedUser.user_id, selectedUser.status !== "active", "管理员操作"))}>{selectedUser.status === "active" ? "禁用用户" : "解禁用户"}</button>
                {selectedUser.role === "admin" ? <button type="button" onClick={() => { const password = window.prompt("输入至少 12 位的新密码"); if (password) void run(() => resetUserPassword(selectedUser.user_id, password)); }}>重置密码</button> : null}
                <button type="button" onClick={() => void createReadonlyKey()}><KeyRound size={14} />创建只读密钥</button>
              </div>
              {serviceSecret ? <div className="secret-once"><strong>服务密钥只显示这一次</strong><code>{serviceSecret}</code></div> : null}
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>设备与服务密钥</h3><span>{selectedUser.keys.length} 个</span></div>
                <div className="compact-list">
                  {selectedUser.keys.map((key) => <div key={key.key_id}><span>{key.name}<small>{key.key_prefix}… · {key.key_type}</small></span><em className={`status-text ${key.status}`}>{key.status === "active" ? "有效" : "已吊销"}</em>{key.status === "active" ? <button className="icon-button" title="吊销密钥" type="button" onClick={() => void run(() => revokeKey(key.key_id))}><Trash2 size={14} /></button> : null}</div>)}
                  {selectedUser.keys.length === 0 ? <p className="admin-empty">暂无密钥</p> : null}
                </div>
              </section>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
