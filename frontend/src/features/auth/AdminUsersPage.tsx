import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  Drawer,
  Dropdown,
  Form,
  Input,
  Menu,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "@arco-design/web-react";
import { IconEye, IconMore, IconPlus, IconUserAdd, IconUserGroup } from "@arco-design/web-react/icon";
import {
  Ban,
  KeyRound,
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
import {
  createAdminKey,
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
  const [createdSecret, setCreatedSecret] = useState("");
  const [createdSecretLabel, setCreatedSecretLabel] = useState("");
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

  const submitUser = (values: {
    username?: string;
    display_name?: string;
    password?: string;
  }) => {
    void run(() => createUser({
      username: String(values.username || ""),
      display_name: String(values.display_name || ""),
      password: String(values.password || ""),
      role: createRole,
    })).then(() => {
      setCreateRole("member");
      setCreateOpen(false);
    });
  };

  const createReadonlyKey = async () => {
    if (!selectedUserId) return;
    try {
      const key: ApiKeyRecord = await createServiceKey(selectedUserId, "QQ 机器人");
      setCreatedSecret(key.secret || "");
      setCreatedSecretLabel("只读服务密钥");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "服务密钥创建失败");
    }
  };

  const createDesktopKey = async () => {
    if (!selectedUserId) return;
    try {
      const key = await createAdminKey(selectedUserId, "监控客户端", "desktop");
      setCreatedSecret(key.secret || "");
      setCreatedSecretLabel("设备密钥");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "设备密钥创建失败");
    }
  };

  const openUser = (userId: string) => {
    setSelectedUserId(userId);
    setCreatedSecret("");
    setCreatedSecretLabel("");
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
    if (action === "create-desktop-key") {
      void createDesktopKey();
      return;
    }
    if (action === "create-service-key") {
      void createReadonlyKey();
      return;
    }
    if (action === "delete-user") {
      void deleteSelectedUser();
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
      width: 72,
      render: (_: unknown, user: AdminUser) => (
        <Tooltip content="查看用户详情">
          <Button aria-label={`查看 ${user.display_name || user.username}`} icon={<IconEye />} shape="square" size="mini" type="text" onClick={() => openUser(user.user_id)} />
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

      <Modal
        className="management-user-modal"
        footer={null}
        getPopupContainer={() => document.querySelector(".admin-shell") || document.body}
        title="创建平台用户"
        unmountOnExit
        visible={createOpen}
        onCancel={() => { setCreateRole("member"); setCreateOpen(false); }}
      >
        <Form className="management-dialog-form" layout="vertical" onSubmit={submitUser}>
          <Form.Item field="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input placeholder="用于登录" />
          </Form.Item>
          <Form.Item field="display_name" label="显示名称">
            <Input placeholder="页面展示名称" />
          </Form.Item>
          <Form.Item label="权限角色">
            <Select value={createRole} onChange={(value) => setCreateRole(value === "admin" ? "admin" : "member")}>
              <Select.Option value="member">普通用户</Select.Option>
              <Select.Option value="admin">管理员</Select.Option>
            </Select>
          </Form.Item>
          {createRole === "admin" ? (
            <Form.Item field="password" label="初始密码" rules={[{ required: true, minLength: 12, message: "请输入至少 12 位密码" }]}>
              <Input.Password placeholder="至少 12 位" />
            </Form.Item>
          ) : null}
          <div className="management-dialog-actions">
            <Button type="secondary" onClick={() => { setCreateRole("member"); setCreateOpen(false); }}>取消</Button>
            <Button htmlType="submit" icon={<IconUserAdd />} type="primary">创建用户</Button>
          </div>
        </Form>
      </Modal>

      <Drawer
        className="management-user-drawer"
        footer={null}
        title={selectedUser ? (
          <div className="management-drawer-header-content">
            <div className="admin-user-heading">
              <span className="admin-user-avatar">{(selectedUser.display_name || selectedUser.username).slice(0, 1).toUpperCase()}</span>
              <div><span className="admin-user-heading-meta">用户详情</span><h2>{selectedUser.display_name || selectedUser.username}</h2><p>@{selectedUser.username} · {selectedUser.role === "admin" ? "管理员" : "普通用户"}</p></div>
            </div>
            <Dropdown
              droplist={(
                <Menu aria-label="用户操作菜单" selectable={false}>
                  <Menu.Item key="toggle-status" onClick={() => runSelectedUserAction("toggle-status")}>{selectedUser.status === "active" ? "禁用用户" : "解禁用户"}</Menu.Item>
                  {selectedUser.role === "admin" ? <Menu.Item key="reset-password" onClick={() => runSelectedUserAction("reset-password")}>重置密码</Menu.Item> : null}
                  <Menu.Item key="create-desktop-key" onClick={() => runSelectedUserAction("create-desktop-key")}><KeyRound size={14} />创建设备密钥</Menu.Item>
                  <Menu.Item key="create-service-key" onClick={() => runSelectedUserAction("create-service-key")}><KeyRound size={14} />创建只读服务密钥</Menu.Item>
                  {selectedUser.user_id !== currentUser?.user_id ? <Menu.Item key="delete-user" onClick={() => runSelectedUserAction("delete-user")}><span className="danger-text"><Trash2 size={14} />删除用户</span></Menu.Item> : null}
                </Menu>
              )}
              popupVisible={actionsOpen}
              position="br"
              trigger="click"
              getPopupContainer={(node) => node.parentElement || document.body}
              onVisibleChange={setActionsOpen}
            >
              <Button aria-expanded={actionsOpen} aria-label="用户操作" className="management-icon-button" icon={<IconMore />} shape="circle" title="用户操作" type="text" />
            </Dropdown>
          </div>
        ) : "用户详情"}
        getPopupContainer={() => document.querySelector(".admin-shell") || document.body}
        visible={Boolean(selectedUser)}
        width={520}
        onCancel={() => { setActionsOpen(false); setSelectedUserId(""); }}
      >
        {selectedUser ? (
          <div className="management-drawer-body">
              <div className="management-drawer-status"><span>账号状态</span><AccountStatusTag status={selectedUser.status} /></div>
              {createdSecret ? <div className="secret-once"><strong>{createdSecretLabel}只显示这一次</strong><code>{createdSecret}</code></div> : null}
              <section className="management-drawer-section">
                <div className="admin-section-heading"><h3>设备与服务密钥</h3><span>{selectedUser.keys.length} 个</span></div>
                <div className="compact-list">
                  {selectedUser.keys.map((key) => <div key={key.key_id}><span>{key.name}<small>{key.key_prefix}… · {key.key_type}</small></span><span className="compact-key-actions"><em className={`status-text ${key.status}`}>{key.status === "active" ? "有效" : "已吊销"}</em>{key.status === "active" ? <Button aria-label={`吊销 ${key.name}`} icon={<Ban size={14} />} shape="circle" size="mini" title="吊销密钥" type="text" onClick={() => void run(() => revokeKey(key.key_id))} /> : null}{key.status === "revoked" && canEnableKey(key) ? <Button aria-label={`重新启用 ${key.name}`} icon={<RotateCcw size={14} />} shape="circle" size="mini" title="重新启用密钥" type="text" onClick={() => void run(() => enableKey(key.key_id))} /> : null}{key.status === "revoked" ? <Button aria-label={`删除 ${key.name}`} icon={<Trash2 size={14} />} shape="circle" size="mini" status="danger" title="永久删除密钥" type="text" onClick={() => { if (window.confirm(`确定永久删除密钥“${key.name}”吗？`)) void run(() => deleteKey(key.key_id)); }} /> : null}</span></div>)}
                  {selectedUser.keys.length === 0 ? <p className="admin-empty">暂无密钥</p> : null}
                </div>
              </section>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
