import { FormEvent, useEffect, useState } from "react";
import { Button, Card, Input, Message, Space, Table, Tag, Typography } from "@arco-design/web-react";
import { IconCopy, IconPlus } from "@arco-design/web-react/icon";
import { Ban, KeyRound, RotateCcw, Trash2 } from "lucide-react";

import {
  KeyStatusTag,
  ManagementError,
  ManagementPageHeader,
  ManagementSummary,
} from "../../components/ManagementPage";
import { createMyKey, deleteKey, enableKey, listMyKeys, revokeKey } from "./api";
import type { ApiKeyRecord } from "./types";

function formatTime(value?: string): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "从未";
}

export function AccountKeysPage() {
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdSecret, setCreatedSecret] = useState("");
  const [error, setError] = useState("");
  const activeKeys = keys.filter((key) => key.status === "active");
  const verifiedKeys = activeKeys.filter((key) => key.identity_verified);
  const canEnable = (key: ApiKeyRecord) => [
    "revoked by user",
    "revoked by administrator",
  ].includes(key.revoked_reason || "");

  const loadKeys = async () => setKeys(await listMyKeys());
  useEffect(() => { void loadKeys().catch((reason) => setError(String(reason))); }, []);

  const createKey = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const key = await createMyKey(newKeyName || "监控客户端");
      setCreatedSecret(key.secret || "");
      setNewKeyName("");
      await loadKeys();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密钥创建失败");
    }
  };

  const runKeyAction = async (action: () => Promise<void>) => {
    setError("");
    try {
      await action();
      await loadKeys();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密钥操作失败");
    }
  };

  const columns = [
    {
      title: "设备名称",
      dataIndex: "name",
      render: (_: unknown, key: ApiKeyRecord) => (
        <Space>
          <span className="arco-key-icon"><KeyRound size={14} /></span>
          <span className="arco-key-name"><strong>{key.name}</strong><small>{key.key_type === "service_readonly" ? "只读服务" : "监控客户端"}</small></span>
        </Space>
      ),
    },
    { title: "密钥前缀", dataIndex: "key_prefix", render: (value: string) => <Typography.Text code>{value}…</Typography.Text> },
    { title: "身份校验", dataIndex: "identity_verified", render: (value: boolean) => <Tag color={value ? "green" : "orange"}>{value ? "已校验" : "等待校验"}</Tag> },
    { title: "状态", dataIndex: "status", render: (value: ApiKeyRecord["status"]) => <KeyStatusTag status={value} /> },
    { title: "最后使用", dataIndex: "last_used_at", render: (value?: string) => formatTime(value) },
    {
      title: "操作",
      render: (_: unknown, key: ApiKeyRecord) => (
        <Space size={4}>
          {key.status === "active" ? <Button aria-label={`吊销 ${key.name}`} icon={<Ban size={14} />} shape="circle" size="mini" title="吊销密钥" type="text" onClick={() => void runKeyAction(() => revokeKey(key.key_id))} /> : null}
          {key.status === "revoked" && canEnable(key) ? <Button aria-label={`重新启用 ${key.name}`} icon={<RotateCcw size={14} />} shape="circle" size="mini" title="重新启用密钥" type="text" onClick={() => void runKeyAction(() => enableKey(key.key_id))} /> : null}
          {key.status === "revoked" ? <Button aria-label={`删除 ${key.name}`} icon={<Trash2 size={14} />} shape="circle" size="mini" status="danger" title="永久删除密钥" type="text" onClick={() => { if (window.confirm(`确定永久删除密钥“${key.name}”吗？`)) void runKeyAction(() => deleteKey(key.key_id)); }} /> : null}
        </Space>
      ),
    },
  ];

  return (
    <div className="account-shell">
      <ManagementPageHeader title="设备密钥" />
      <ManagementError error={error} />
      <ManagementSummary ariaLabel="密钥摘要" items={[
        { label: "有效密钥", value: activeKeys.length },
        { label: "身份已校验", value: verifiedKeys.length },
        { label: "等待校验", value: activeKeys.length - verifiedKeys.length },
      ]} />

      <section className="account-grid account-grid-single">
        <Card className="account-panel arco-management-card" title={<Space><KeyRound size={17} />客户端访问凭据</Space>}>
          <form className="inline-form account-key-form" onSubmit={createKey}>
            <Input maxLength={80} placeholder="设备名称" value={newKeyName} onChange={setNewKeyName} />
            <Button htmlType="submit" icon={<IconPlus />} type="primary">创建设备密钥</Button>
          </form>
          {createdSecret ? (
            <div className="secret-once" role="status">
              <strong>密钥只显示这一次</strong>
              <code>{createdSecret}</code>
              <Button icon={<IconCopy />} size="small" type="outline" onClick={() => void navigator.clipboard.writeText(createdSecret).then(() => Message.success("密钥已复制"))}>复制密钥</Button>
            </div>
          ) : null}
          <Table<ApiKeyRecord> border={false} columns={columns} data={keys} noDataElement="尚未创建设备密钥" pagination={false} rowKey="key_id" />
        </Card>
      </section>
    </div>
  );
}
