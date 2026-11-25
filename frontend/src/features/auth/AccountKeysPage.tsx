import { useEffect, useState } from "react";
import { Button, Card, Input, Pagination, Space, Table, Typography } from "@arco-design/web-react";
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

const ACCOUNT_KEY_PAGE_SIZE = 20;
const KEY_TYPE_LABELS: Record<ApiKeyRecord["key_type"], string> = {
  local_client: "独立版客户端",
  seat: "Seat 客户端",
  service_readonly: "只读服务",
  desktop: "历史监控客户端",
};

export function AccountKeysPage() {
  const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [localIssuance, setLocalIssuance] = useState(false);
  const [deviceName, setDeviceName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createdSecret, setCreatedSecret] = useState("");
  const activeKeys = keys.filter((key) => key.status === "active");
  const canEnable = (key: ApiKeyRecord) => [
    "revoked by user",
    "revoked by administrator",
  ].includes(key.revoked_reason || "");

  const loadKeys = async () => {
    setLoading(true);
    try {
      const snapshot = await listMyKeys();
      setKeys(snapshot.keys);
      setLocalIssuance(snapshot.localIssuance);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void loadKeys().catch((reason) => setError(String(reason))); }, []);
  const pagedKeys = keys.slice((page - 1) * ACCOUNT_KEY_PAGE_SIZE, page * ACCOUNT_KEY_PAGE_SIZE);
  useEffect(() => {
    setPage((current) => Math.min(current, Math.max(1, Math.ceil(keys.length / ACCOUNT_KEY_PAGE_SIZE))));
  }, [keys.length]);

  const runKeyAction = async (action: () => Promise<void>) => {
    setError("");
    try {
      await action();
      await loadKeys();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密钥操作失败");
    }
  };

  const createKey = async () => {
    const name = deviceName.trim();
    if (!name || creating) return;
    setError("");
    setCreating(true);
    try {
      const { secret, ...key } = await createMyKey(name);
      setCreatedSecret(secret);
      setKeys((current) => [key, ...current]);
      setPage(1);
      setDeviceName("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建设备密钥失败");
    } finally {
      setCreating(false);
    }
  };

  const columns = [
    {
      title: "设备名称",
      dataIndex: "name",
      render: (_: unknown, key: ApiKeyRecord) => (
        <Space>
          <span className="arco-key-icon"><KeyRound size={14} /></span>
          <span className="arco-key-name"><strong>{key.name}</strong><small>{KEY_TYPE_LABELS[key.key_type]}</small></span>
        </Space>
      ),
    },
    { title: "密钥前缀", dataIndex: "key_prefix", render: (value: string) => <Typography.Text code>{value}…</Typography.Text> },
    { title: "状态", dataIndex: "status", render: (value: ApiKeyRecord["status"]) => <KeyStatusTag status={value} /> },
    { title: "最后使用", dataIndex: "last_used_at", render: (value?: string) => formatTime(value) },
    {
      title: "操作",
      render: (_: unknown, key: ApiKeyRecord) => (
        <Space size={4}>
          {key.key_type !== "seat" && key.status === "active" ? <Button aria-label={`吊销 ${key.name}`} icon={<Ban size={14} />} shape="circle" size="mini" title="吊销密钥" type="text" onClick={() => void runKeyAction(() => revokeKey(key.key_id))} /> : null}
          {key.key_type !== "seat" && key.status === "revoked" && canEnable(key) ? <Button aria-label={`重新启用 ${key.name}`} icon={<RotateCcw size={14} />} shape="circle" size="mini" title="重新启用密钥" type="text" onClick={() => void runKeyAction(() => enableKey(key.key_id))} /> : null}
          {key.key_type !== "seat" && key.status === "revoked" ? <Button aria-label={`删除 ${key.name}`} icon={<Trash2 size={14} />} shape="circle" size="mini" status="danger" title="永久删除密钥" type="text" onClick={() => { if (window.confirm(`确定永久删除密钥“${key.name}”吗？`)) void runKeyAction(() => deleteKey(key.key_id)); }} /> : null}
        </Space>
      ),
    },
  ];

  return (
    <div className="account-shell">
      <ManagementPageHeader title="设备密钥" />
      <ManagementError error={error} />
      <ManagementSummary ariaLabel="密钥摘要" items={[
        { label: "密钥总数", value: keys.length },
        { label: "有效密钥", value: activeKeys.length },
      ]} />

      <section className="account-grid account-grid-single">
        <Card className="account-key-card arco-management-card" title={<Space><KeyRound size={17} />客户端访问凭据</Space>}>
          {localIssuance ? (
            createdSecret ? (
              <Space direction="vertical" style={{ width: "100%" }}>
                <label htmlFor="created-device-key">设备密钥（仅显示一次）</label>
                <Input id="created-device-key" value={createdSecret} readOnly />
                <Button onClick={() => setCreatedSecret("")}>完成</Button>
              </Space>
            ) : (
              <form className="account-key-form" onSubmit={(event) => {
                event.preventDefault();
                void createKey();
              }}>
                <label htmlFor="new-device-name">设备名称</label>
                <Input id="new-device-name" value={deviceName} onChange={setDeviceName} maxLength={80} disabled={creating} />
                <Button htmlType="submit" type="primary" loading={creating} disabled={!deviceName.trim()}>创建设备密钥</Button>
              </form>
            )
          ) : !loading ? <p className="management-hint">客户端密钥统一由 GloryNavy_Seat 签发、绑定和吊销。</p> : null}
          <Table<ApiKeyRecord> border={false} columns={columns} data={pagedKeys} loading={loading} noDataElement={localIssuance ? "暂无设备密钥" : "暂无已接入的 Seat 密钥"} pagination={false} rowKey="key_id" />
          {keys.length > ACCOUNT_KEY_PAGE_SIZE ? <Pagination current={page} pageSize={ACCOUNT_KEY_PAGE_SIZE} showTotal total={keys.length} onChange={setPage} /> : null}
        </Card>
      </section>
    </div>
  );
}
