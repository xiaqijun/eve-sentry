import { useEffect, useMemo, useState, type FormEvent } from "react";
import { DropdownMenu } from "radix-ui";
import { Button, Card, Drawer, Field, FormDialog, Input, Pagination, Select, Space, Table, Tag, Tooltip, Typography } from "@/components/ui";
import { Eye as IconEye, Ellipsis as IconMore, Plus as IconPlus, Users as IconUserGroup } from "lucide-react";
import {
  Ban,
  RotateCcw,
  Trash2,
} from "lucide-react";

import {
  AccountStatusTag,
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
  UserIdentity,
} from "../../components/ManagementPage";
import { useOperationFeedback } from "../../components/operationFeedback";
import {
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
const USER_PAGE_SIZE = 20;

export function AdminUsersPage() {
  const feedback = useOperationFeedback();
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<UserStatusFilter>("all");
  const [page, setPage] = useState(1);
  const [createRole, setCreateRole] = useState<"admin" | "member">("member");
  const [createOpen, setCreateOpen] = useState(false);
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
  const pagedUsers = filteredUsers.slice((page - 1) * USER_PAGE_SIZE, page * USER_PAGE_SIZE);
  useEffect(() => {
    setPage(1);
  }, [search, statusFilter]);
  useEffect(() => {
    const lastPage = Math.max(1, Math.ceil(filteredUsers.length / USER_PAGE_SIZE));
    setPage((current) => Math.min(current, lastPage));
  }, [filteredUsers.length]);

  const run = async (action: () => Promise<unknown>): Promise<boolean> => {
    setError("");
    try {
      await action();
      await load();
      return true;
    } catch (reason) {
      feedback.operationError(reason, "操作失败");
      return false;
    }
  };

  const submitUser = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    void run(() => createUser({
      username: String(fields.get("username") || ""),
      display_name: String(fields.get("display_name") || ""),
      password: String(fields.get("password") || ""),
      role: createRole,
    })).then((success) => {
      if (success) {
        setCreateRole("member");
        setCreateOpen(false);
      }
    });
  };

  const openUser = (userId: string) => {
    setSelectedUserId(userId);
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
      feedback.operationError(reason, "删除用户失败");
    }
  };

  const runSelectedUserAction = (action: string) => {
    if (!selectedUser) return;
    setActionsOpen(false);
    if (action === "toggle-status") {
      void run(() => setUserActive(
        selectedUser.user_id,
        selectedUser.status !== "active",
        "管理员操作",
      ));
      return;
    }
    if (action === "reset-password") {
      const password = window.prompt("输入至少 12 位的新密码");
      if (password) void run(() => resetUserPassword(selectedUser.user_id, password));
      return;
    }
    if (action === "delete-user") {
      void deleteSelectedUser();
    }
  };

  const columns = [
    { title: "用户", render: (_: unknown, user: AdminUser) => <UserIdentity displayName={user.display_name} username={user.username} /> },
    { title: "角色", dataIndex: "role", render: (role: AdminUser["role"]) => <Tag color={role === "admin" ? "blue" : "gray"}>{role === "admin" ? "管理员" : "普通用户"}</Tag> },
    { title: "状态", dataIndex: "status", render: (status: AdminUser["status"]) => <AccountStatusTag status={status} /> },
    { title: "有效密钥", dataIndex: "keys", render: (keys: AdminUser["keys"]) => keys.filter((key) => key.status === "active").length },
    {
      title: "操作",
      width: 72,
      render: (_: unknown, user: AdminUser) => (
        <Tooltip content="查看用户详情">
          <Button aria-label={`查看 ${user.display_name || user.username}`} icon={<IconEye />} shape="square" size="xs" variant="ghost" onClick={() => openUser(user.user_id)} />
        </Tooltip>
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
        className="management-table-panel ui-management-card"
        extra={(
          <Space wrap>
            <Input.Search aria-label="搜索用户" placeholder="搜索用户名或显示名称" value={search} onChange={setSearch} />
            <Select label="筛选用户状态" value={statusFilter} className="w-[120px]" onValueChange={(value) => setStatusFilter(value as UserStatusFilter)}
              options={[{ value: "all", label: "全部状态" }, { value: "active", label: "正常" }, { value: "disabled", label: "已禁用" }]} />
            <Button icon={<IconPlus />} variant="default" onClick={() => setCreateOpen(true)}>新建用户</Button>
          </Space>
        )}
        title={<Space><IconUserGroup /><span>用户列表</span><Typography.Text type="secondary">共 {filteredUsers.length} 个结果</Typography.Text></Space>}
      >
        <Table<AdminUser> border={false} columns={columns} data={pagedUsers} loading={loading} noDataElement="没有符合条件的用户" pagination={false} rowKey="user_id" />
        {filteredUsers.length > USER_PAGE_SIZE ? <Pagination current={page} pageSize={USER_PAGE_SIZE} showTotal total={filteredUsers.length} onChange={setPage} /> : null}
      </Card>

      {createOpen ? <FormDialog
        className="management-dialog-form"
        title="创建平台用户"
        submitLabel="创建用户"
        close={() => { setCreateRole("member"); setCreateOpen(false); }}
        onSubmit={submitUser}
      >
        <Field label="用户名"><Input name="username" placeholder="用于登录" required /></Field>
        <Field label="显示名称"><Input name="display_name" placeholder="页面展示名称" /></Field>
        <div className="ui-form-item">
          <span className="ui-form-label-item">权限角色</span>
          <Select label="权限角色" value={createRole} onValueChange={(value) => setCreateRole(value === "admin" ? "admin" : "member")}
            options={[{ value: "member", label: "普通用户" }, { value: "admin", label: "管理员" }]} />
        </div>
        <Field label="初始密码"><Input.Password name="password" placeholder="至少 12 位" required minLength={12} autoComplete="new-password" /></Field>
      </FormDialog> : null}

      <Drawer
        className="management-user-drawer"
        label="用户详情"
        footer={null}
        title={selectedUser ? (
          <div className="management-drawer-header-content">
            <div className="admin-user-heading">
              <span className="admin-user-avatar">{(selectedUser.display_name || selectedUser.username).slice(0, 1).toUpperCase()}</span>
              <div><span className="admin-user-heading-meta">用户详情</span><h2>{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username} · {selectedUser.role === "admin" ? "管理员" : "普通用户"}</p></div>
            </div>
            <DropdownMenu.Root open={actionsOpen} onOpenChange={setActionsOpen}>
              <DropdownMenu.Trigger asChild>
                <Button aria-label="用户操作" className="management-icon-button" icon={<IconMore />} shape="circle" title="用户操作" variant="ghost" />
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content className="ui-menu-content" align="end" sideOffset={4} aria-label="用户操作菜单">
                  <DropdownMenu.Item className="ui-menu-item" onSelect={() => runSelectedUserAction("toggle-status")}>{selectedUser.status === "active" ? "禁用用户" : "解禁用户"}</DropdownMenu.Item>
                  {selectedUser.role === "admin" ? <DropdownMenu.Item className="ui-menu-item" onSelect={() => runSelectedUserAction("reset-password")}>重置密码</DropdownMenu.Item> : null}
                  {selectedUser.user_id !== currentUser?.user_id ? <DropdownMenu.Item className="ui-menu-item danger-text" onSelect={() => runSelectedUserAction("delete-user")}><Trash2 size={14} />删除用户</DropdownMenu.Item> : null}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
          </div>
        ) : "用户详情"}
        open={Boolean(selectedUser)}
        width={520}
        onClose={() => { setActionsOpen(false); setSelectedUserId(""); }}
      >
        {selectedUser ? (
          <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><AccountStatusTag status={selectedUser.status} /></div>
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>Seat 客户端密钥</h3><span>{selectedUser.keys.length} 个</span></div>
                <div className="compact-list">
                  {selectedUser.keys.map((key) => <div key={key.key_id}><span>{key.name}<small>{key.key_prefix}… · {key.key_type}</small></span><span className="compact-key-actions"><em className={`status-text ${key.status}`}>{key.status === "active" ? "有效" : "已吊销"}</em>{key.key_type !== "seat" && key.status === "active" ? <Button aria-label={`吊销 ${key.name}`} icon={<Ban size={14} />} shape="circle" size="xs" title="吊销密钥" variant="ghost" onClick={() => void run(() => revokeKey(key.key_id))} /> : null}{key.key_type !== "seat" && key.status === "revoked" && canEnableKey(key) ? <Button aria-label={`重新启用 ${key.name}`} icon={<RotateCcw size={14} />} shape="circle" size="xs" title="重新启用密钥" variant="ghost" onClick={() => void run(() => enableKey(key.key_id))} /> : null}{key.key_type !== "seat" && key.status === "revoked" ? <Button aria-label={`删除 ${key.name}`} icon={<Trash2 size={14} />} shape="circle" size="xs" status="danger" title="永久删除密钥" variant="ghost" onClick={() => { if (window.confirm(`确定永久删除密钥“${key.name}”吗？`)) void run(() => deleteKey(key.key_id)); }} /> : null}</span></div>)}
                  {selectedUser.keys.length === 0 ? <p className="admin-empty">暂无密钥</p> : null}
                </div>
              </section>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
