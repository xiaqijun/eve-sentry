import { FormEvent, useEffect, useMemo, useState } from "react";
import { Button, Card, Input, Select, Space, Table, Tag, Typography } from "@arco-design/web-react";
import { IconEye, IconMore, IconPlus, IconUserAdd, IconUserGroup } from "@arco-design/web-react/icon";
import {
  Ban,
  KeyRound,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";

import {
  AccountStatusTag,
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
  UserIdentity,
} from "../../components/ManagementPage";
import {
  createServiceKey,
  createUser,
  deleteKey,
  deleteUser,
  enableKey,
  listAdminUsers,
  resetUserPassword,
  revokeKey,
  setUserActive,
} from "./api";
import { useAuth } from "./AuthContext";
import type { AdminUser, ApiKeyRecord } from "./types";

type UserStatusFilter = "all" | "active" | "disabled";

export function AdminUsersPage() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<UserStatusFilter>("all");
  const [createRole, setCreateRole] = useState<"admin" | "member">("member");
  const [createOpen, setCreateOpen] = useState(false);
  const [serviceSecret, setServiceSecret] = useState("");
  const [actionsOpen, setActionsOpen] = useState(false);
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
    setActionsOpen(false);
  };

  const canEnableKey = (key: ApiKeyRecord) => [
    "revoked by user",
    "revoked by administrator",
  ].includes(key.revoked_reason || "");

  const deleteSelectedUser = async () => {
    if (!selectedUser || selectedUser.user_id === currentUser?.user_id) return;
    const name = selectedUser.display_name || selectedUser.username;
    if (!window.confirm(`确定删除用户“${name}”吗？该用户的密钥和角色记录也会删除。`)) {
      return;
    }
    setError("");
    try {
      await deleteUser(selectedUser.user_id);
      setSelectedUserId("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除用户失败");
    }
  };

  const columns = [
    { title: "用户", render: (_: unknown, user: AdminUser) => <UserIdentity displayName={user.display_name} username={user.username} /> },
    { title: "角色", dataIndex: "role", render: (role: AdminUser["role"]) => <Tag color={role === "admin" ? "arcoblue" : "gray"}>{role === "admin" ? "管理员" : "普通用户"}</Tag> },
    { title: "状态", dataIndex: "status", render: (status: AdminUser["status"]) => <AccountStatusTag status={status} /> },
    { title: "有效密钥", dataIndex: "keys", render: (keys: AdminUser["keys"]) => keys.filter((key) => key.status === "active").length },
    { title: "已验证角色", dataIndex: "verified_characters", render: (items: AdminUser["verified_characters"]) => items.length },
    {
      title: "操作",
      render: (_: unknown, user: AdminUser) => (
        <Button aria-label={`查看 ${user.display_name || user.username}`} icon={<IconEye />} size="small" title="查看详情" type="text" onClick={() => openUser(user.user_id)}>查看</Button>
      ),
    },
  ];

  return (
    <div className="admin-shell">
      <ManagementPageHeader loading={loading} refreshLabel="刷新数据" title="用户管理" onRefresh={() => void load()} />
      <ManagementError error={error} />
      <ManagementSummary ariaLabel="用户管理摘要" items={[
        { label: "平台用户", value: users.length },
        { label: "正常用户", value: activeUsers },
        { label: "有效密钥", value: activeKeys },
      ]} />

      <Card
        className="management-table-panel arco-management-card"
        extra={(
          <Space wrap>
            <Input.Search aria-label="搜索用户" placeholder="搜索用户名或显示名称" value={search} onChange={setSearch} />
            <Select aria-label="筛选用户状态" value={statusFilter} style={{ width: 120 }} onChange={(value) => setStatusFilter(value as UserStatusFilter)}>
              <Select.Option value="all">全部状态</Select.Option>
              <Select.Option value="active">正常</Select.Option>
              <Select.Option value="disabled">已禁用</Select.Option>
            </Select>
            <Button icon={<IconPlus />} type="primary" onClick={() => setCreateOpen(true)}>新建用户</Button>
          </Space>
        )}
        title={<Space><IconUserGroup /><span>用户列表</span><Typography.Text type="secondary">共 {filteredUsers.length} 个结果</Typography.Text></Space>}
      >
        <Table<AdminUser> border={false} columns={columns} data={filteredUsers} loading={loading} noDataElement="没有符合条件的用户" pagination={false} rowKey="user_id" />
      </Card>

      {createOpen ? (
        <div className="management-modal-backdrop" role="presentation" onMouseDown={() => setCreateOpen(false)}>
          <section aria-labelledby="create-user-title" aria-modal="true" className="management-modal" role="dialog" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span>新建账号</span><h2 id="create-user-title">创建平台用户</h2></div><button aria-label="关闭" className="management-close-button" type="button" onClick={() => setCreateOpen(false)}><X size={17} /></button></header>
            <form className="management-dialog-form" onSubmit={submitUser}>
              <label><span>用户名</span><Input name="username" placeholder="用于登录" required /></label>
              <label><span>显示名称</span><Input name="display_name" placeholder="页面展示名称" /></label>
              <label><span>权限角色</span><Select value={createRole} onChange={(value) => setCreateRole(value === "admin" ? "admin" : "member")}><Select.Option value="member">普通用户</Select.Option><Select.Option value="admin">管理员</Select.Option></Select></label>
              {createRole === "admin" ? <label><span>初始密码</span><Input.Password minLength={12} name="password" placeholder="至少 12 位" required /></label> : null}
              <footer><Button type="secondary" onClick={() => setCreateOpen(false)}>取消</Button><Button htmlType="submit" icon={<IconUserAdd />} type="primary">创建用户</Button></footer>
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
              <div className="management-drawer-header-actions">
                <Button aria-expanded={actionsOpen} aria-label="用户操作" className="management-icon-button" icon={<IconMore />} shape="circle" title="用户操作" type="text" onClick={() => setActionsOpen((current) => !current)} />
                <Button aria-label="关闭详情" className="management-close-button" icon={<X size={17} />} shape="circle" type="text" onClick={() => { setActionsOpen(false); setSelectedUserId(""); }} />
                {actionsOpen ? (
                  <div aria-label="用户操作菜单" className="management-actions-menu" role="menu">
                    <Button role="menuitem" type="text" onClick={() => { setActionsOpen(false); void run(() => setUserActive(selectedUser.user_id, selectedUser.status !== "active", "管理员操作")); }}>{selectedUser.status === "active" ? "禁用用户" : "解禁用户"}</Button>
                    {selectedUser.role === "admin" ? <Button role="menuitem" type="text" onClick={() => { setActionsOpen(false); const password = window.prompt("输入至少 12 位的新密码"); if (password) void run(() => resetUserPassword(selectedUser.user_id, password)); }}>重置密码</Button> : null}
                    <Button icon={<KeyRound size={14} />} role="menuitem" type="text" onClick={() => { setActionsOpen(false); void createReadonlyKey(); }}>创建只读密钥</Button>
                    {selectedUser.user_id !== currentUser?.user_id ? <Button className="danger-action" icon={<Trash2 size={14} />} role="menuitem" status="danger" type="text" onClick={() => { setActionsOpen(false); void deleteSelectedUser(); }}>删除用户</Button> : null}
                  </div>
                ) : null}
              </div>
            </header>
            <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><AccountStatusTag status={selectedUser.status} /></div>
              {serviceSecret ? <div className="secret-once"><strong>服务密钥只显示这一次</strong><code>{serviceSecret}</code></div> : null}
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>设备与服务密钥</h3><span>{selectedUser.keys.length} 个</span></div>
                <div className="compact-list">
                  {selectedUser.keys.map((key) => <div key={key.key_id}><span>{key.name}<small>{key.key_prefix}… · {key.key_type}</small></span><span className="compact-key-actions"><em className={`status-text ${key.status}`}>{key.status === "active" ? "有效" : "已吊销"}</em>{key.status === "active" ? <button className="icon-button" title="吊销密钥" type="button" onClick={() => void run(() => revokeKey(key.key_id))}><Ban size={14} /></button> : null}{key.status === "revoked" && canEnableKey(key) ? <button className="icon-button" title="重新启用密钥" type="button" onClick={() => void run(() => enableKey(key.key_id))}><RotateCcw size={14} /></button> : null}{key.status === "revoked" ? <button className="icon-button danger-text" title="永久删除密钥" type="button" onClick={() => { if (window.confirm(`确定永久删除密钥“${key.name}”吗？`)) void run(() => deleteKey(key.key_id)); }}><Trash2 size={14} /></button> : null}</span></div>)}
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
