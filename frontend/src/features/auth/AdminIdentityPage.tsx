import { useEffect, useMemo, useState } from "react";
import { Button, Card, Descriptions, Drawer, Empty, Input, List, Space, Table, Tooltip, Typography } from "@arco-design/web-react";
import { IconEye, IconIdcard } from "@arco-design/web-react/icon";

import {
  AccountStatusTag,
  ManagementError,
  ManagementPageHeader,
  UserIdentity,
} from "../../components/ManagementPage";
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

  const columns = [
    { title: "用户", render: (_: unknown, user: AdminUser) => <UserIdentity displayName={user.display_name} username={user.username} /> },
    { title: "已验证角色", dataIndex: "verified_characters", render: (items: AdminUser["verified_characters"]) => items.length },
    { title: "账号状态", dataIndex: "status", render: (status: AdminUser["status"]) => <AccountStatusTag status={status} /> },
    {
      title: "操作",
      width: 72,
      render: (_: unknown, user: AdminUser) => (
        <Tooltip content="查看身份详情">
          <Button aria-label={`查看 ${user.display_name || user.username} 身份`} icon={<IconEye />} shape="square" size="mini" type="text" onClick={() => setSelectedUserId(user.user_id)} />
        </Tooltip>
      ),
    },
  ];

  return (
    <div className="admin-shell">
      <ManagementPageHeader loading={loading} refreshLabel="刷新数据" title="身份记录" onRefresh={() => void load()} />
      <ManagementError error={error} />

      <Card
        className="management-table-panel arco-management-card"
        extra={<Input.Search aria-label="搜索用户身份" placeholder="搜索用户名或显示名称" value={search} onChange={setSearch} />}
        title={<Space><IconIdcard /><span>已验证身份</span><Typography.Text type="secondary">共 {filteredUsers.length} 个结果</Typography.Text></Space>}
      >
        <Table<AdminUser> border={false} columns={columns} data={filteredUsers} loading={loading} noDataElement="没有符合条件的用户" pagination={false} rowKey="user_id" />
      </Card>

      <Drawer
        footer={null}
        title={selectedUser ? <UserIdentity displayName={selectedUser.display_name} username={selectedUser.username} /> : "身份详情"}
        visible={Boolean(selectedUser)}
        width={440}
        onCancel={() => setSelectedUserId("")}
      >
        {selectedUser ? (
          <Space direction="vertical" size={18} style={{ width: "100%" }}>
            <Descriptions border data={[{ label: "账号状态", value: <AccountStatusTag status={selectedUser.status} /> }]} column={1} size="small" />
            <section>
              <Typography.Title heading={6}>已验证角色 · {selectedUser.verified_characters.length}</Typography.Title>
              {selectedUser.verified_characters.length ? (
                <List
                  dataSource={selectedUser.verified_characters}
                  render={(item) => <List.Item key={item.character_id}><List.Item.Meta title={item.character_name} description={String(item.corporation_name || item.corporation_id || "未知军团")} /></List.Item>}
                />
              ) : <Empty description="尚未发现已验证角色" />}
            </section>
          </Space>
        ) : null}
      </Drawer>
    </div>
  );
}
