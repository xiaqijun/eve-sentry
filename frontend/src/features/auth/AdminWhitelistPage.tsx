import { FormEvent, useEffect, useMemo, useState } from "react";
import { Button, Card, Drawer, Empty, Input, List, Select, Space, Table, Tooltip, Typography } from "@arco-design/web-react";
import { IconDelete, IconPlus, IconSafe, IconSettings, IconUserGroup } from "@arco-design/web-react/icon";

import {
  ManagementError,
  ManagementPageHeader,
  UserIdentity,
} from "../../components/ManagementPage";
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
  const availableCharacters = useMemo(() => {
    if (!selectedUser) return [];
    const whitelistedIds = new Set(selectedUser.whitelist.map((item) => item.character_id));
    return selectedUser.verified_characters.filter(
      (character) => !whitelistedIds.has(character.character_id),
    );
  }, [selectedUser]);
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

  const corporationColumns = [
    {
      title: "军团",
      render: (_: unknown, corporation: AllowedCorporation) => (
        <Space><IconSafe /><span><strong>{corporation.corporation_name || "未知军团"}</strong><small className="arco-table-subtext">ID {corporation.corporation_id}</small></span></Space>
      ),
    },
    {
      title: "操作",
      width: 100,
      render: (_: unknown, corporation: AllowedCorporation) => (
        <Button aria-label={`移除 ${corporation.corporation_name || corporation.corporation_id}`} icon={<IconDelete />} size="small" status="danger" title="移除" type="text" onClick={() => void run(() => removeCorporation(corporation.corporation_id))} />
      ),
    },
  ];

  const userColumns = [
    { title: "用户", render: (_: unknown, user: AdminUser) => <UserIdentity displayName={user.display_name} username={user.username} /> },
    { title: "白名单角色", dataIndex: "whitelist", render: (items: AdminUser["whitelist"]) => items.length },
    {
      title: "操作",
      width: 72,
      render: (_: unknown, user: AdminUser) => (
        <Tooltip content="管理白名单">
          <Button aria-label={`管理 ${user.display_name || user.username} 白名单`} icon={<IconSettings />} shape="square" size="mini" type="text" onClick={() => setSelectedUserId(user.user_id)} />
        </Tooltip>
      ),
    },
  ];

  return (
    <div className="admin-shell">
      <ManagementPageHeader loading={loading} title="白名单管理" onRefresh={() => void load()} />
      <ManagementError error={error} />

      <Card
        className="management-table-panel arco-management-card"
        extra={(
          <form className="management-policy-form" onSubmit={submitCorporation}>
            <Input aria-label="军团 ID" inputMode="numeric" placeholder="军团 ID" required value={corporationId} onChange={setCorporationId} />
            <Button htmlType="submit" icon={<IconPlus />} type="primary">添加</Button>
          </form>
        )}
        title={<Space><IconSafe /><span>军团白名单</span></Space>}
      >
        <Table<AllowedCorporation> border={false} columns={corporationColumns} data={corporations} loading={loading} noDataElement="暂无军团" pagination={false} rowKey="corporation_id" />
      </Card>

      <Card
        className="management-table-panel arco-management-card"
        extra={<Input.Search aria-label="搜索用户" placeholder="搜索用户" value={search} onChange={setSearch} />}
        title={<Space><IconUserGroup /><span>角色白名单</span><Typography.Text type="secondary">共 {filteredUsers.length} 个用户</Typography.Text></Space>}
      >
        <Table<AdminUser> border={false} columns={userColumns} data={filteredUsers} loading={loading} noDataElement="暂无用户" pagination={false} rowKey="user_id" />
      </Card>

      <Drawer footer={null} title={selectedUser ? <UserIdentity displayName={selectedUser.display_name} username={selectedUser.username} /> : "白名单详情"} visible={Boolean(selectedUser)} width={460} onCancel={() => setSelectedUserId("")}>
        {selectedUser ? (
          <Space direction="vertical" size={18} style={{ width: "100%" }}>
            <Typography.Title heading={6}>角色白名单 · {selectedUser.whitelist.length}</Typography.Title>
            <form className="identity-character-form" onSubmit={submitCharacter}>
              <Select
                aria-label="选择已验证上报角色"
                allowClear
                disabled={availableCharacters.length === 0}
                placeholder={availableCharacters.length ? "从已验证上报角色中选择" : "没有可加白的已验证角色"}
                showSearch
                value={characterId || undefined}
                onChange={(value) => setCharacterId(String(value || ""))}
              >
                {availableCharacters.map((character) => (
                  <Select.Option key={character.character_id} value={String(character.character_id)}>
                    {character.character_name} · {character.corporation_name || "未知军团"}
                  </Select.Option>
                ))}
              </Select>
              <Input aria-label="备注" placeholder="备注（可选）" value={characterNote} onChange={setCharacterNote} />
              <Button disabled={!characterId} htmlType="submit" icon={<IconPlus />} type="primary">加白</Button>
            </form>
            {selectedUser.whitelist.length ? (
              <List
                dataSource={selectedUser.whitelist}
                render={(item) => (
                  <List.Item key={item.character_id} extra={<Button aria-label={`移除 ${item.character_name}`} icon={<IconDelete />} status="danger" title="移除" type="text" onClick={() => void run(() => removeWhitelistCharacter(selectedUser.user_id, item.character_id))} />}>
                    <List.Item.Meta title={item.character_name} description={`ID ${item.character_id}${item.note ? ` · ${item.note}` : ""}`} />
                  </List.Item>
                )}
              />
            ) : <Empty description="暂无角色" />}
          </Space>
        ) : null}
      </Drawer>
    </div>
  );
}
