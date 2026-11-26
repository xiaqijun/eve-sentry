import { useCallback, useEffect, useState } from "react";
import { Button, Card, Space, Switch, Tag, Typography } from "@/components/ui";
import { Copy as IconCopy } from "lucide-react";
import { copyToClipboard } from "../../lib/clipboard";

import { ManagementError, ManagementPageHeader } from "../../components/ManagementPage";
import { useOperationFeedback } from "../../components/operationFeedback";
import { apiPath, fetchCurrentSeatServiceToken, fetchSeatIntegration, saveSeatServiceToken, updateSeatIntegration } from "./api";
import type { SeatIntegrationSettings } from "./types";

export function AdminSeatIntegrationPage() {
  const feedback = useOperationFeedback();
  const [settings, setSettings] = useState<SeatIntegrationSettings | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [copying, setCopying] = useState(false);
  const [error, setError] = useState("");
  const sentryOrigin = new URL(apiPath("/"), window.location.origin).origin;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await fetchSeatIntegration();
      setSettings(next);
      setEnabled(next.enabled);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取 SeAT 设置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function save() {
    if (!settings || saving) return;
    setSaving(true);
    setError("");
    try {
      const next = await updateSeatIntegration(enabled, settings.revision);
      setSettings(next);
      setEnabled(next.enabled);
      feedback.success(next.enabled ? "已切换至 SeAT 版" : "已切换至独立版");
    } catch (reason) {
      feedback.operationError(reason, "保存 SeAT 设置失败");
    } finally {
      setSaving(false);
    }
  }

  async function saveToken() {
    if (!settings || saving) return;
    setSaving(true);
    setError("");
    try {
      setSettings(await saveSeatServiceToken(settings.revision));
      feedback.success("令牌已更新");
    } catch (reason) {
      feedback.operationError(reason, "保存服务令牌失败");
    } finally {
      setSaving(false);
    }
  }

  async function copyToken() {
    setCopying(true);
    setError("");
    try {
      const token = await fetchCurrentSeatServiceToken();
      await copyToClipboard(token);
      feedback.success("令牌已复制");
    } catch (reason) {
      feedback.operationError(reason instanceof DOMException ? undefined : reason, "复制令牌失败");
    } finally {
      setCopying(false);
    }
  }

  async function copyAddress() {
    try {
      await copyToClipboard(sentryOrigin);
      feedback.success("Sentry 地址已复制");
    } catch {
      feedback.operationError(undefined, "复制地址失败");
    }
  }

  const busy = loading || saving || copying;
  const canSave = settings?.editable
    && (settings.enabled !== enabled || (!enabled && !settings.local_issuance))
    && (!enabled || settings.token_configured);

  return (
    <section className="content-page account-page ui-management-page seat-integration-page">
      <ManagementPageHeader
        title="SeAT 接入"
        loading={loading}
        extra={<Button disabled={busy} onClick={() => void load()}>刷新状态</Button>}
      />
      <ManagementError error={error} />
      <Card className="ui-management-card" title="运行模式" loading={loading}>
        {settings ? (
          <Space direction="vertical" size="medium">
            <Tag color={settings.enabled ? "green" : "gray"}>
              {settings.enabled ? "SeAT 版" : settings.local_issuance ? "独立版" : "启动配置"}
            </Tag>
            <Space wrap>
              <Typography.Text>启用 SeAT</Typography.Text>
              <Switch aria-label="启用 SeAT" checked={enabled} disabled={busy || !settings.editable}
                onChange={setEnabled} />
              <Button variant="default" disabled={busy || !canSave} loading={saving} onClick={() => void save()}>
                保存模式
              </Button>
            </Space>
            {!settings.editable ? <Tag>只读</Tag> : null}
            {enabled && !settings.token_configured
              ? <Typography.Text type="warning">请先更新令牌</Typography.Text>
              : null}
            {settings.enabled ? (
              <Typography.Text>
                {settings.last_authenticated_at
                  ? "最近一次通过服务鉴权：" + new Date(settings.last_authenticated_at).toLocaleString("zh-CN", { hour12: false })
                  : "尚未收到 SeAT 服务鉴权请求"}
              </Typography.Text>
            ) : null}
          </Space>
        ) : null}
      </Card>
      <Card
        className="ui-management-card"
        title="服务令牌"
        extra={settings && <Tag color={settings.token_copy_available ? "green" : settings.token_configured ? "orange" : "gray"}>
          {settings.token_copy_available ? "已配置" : settings.token_configured ? "待恢复" : "未配置"}
        </Tag>}
      >
        <Space wrap>
          {settings?.token_configured && <Typography.Text code aria-label="已隐藏的服务令牌">••••••••</Typography.Text>}
          <Button variant="default" disabled={busy || !settings?.editable || !settings.token_copy_available}
            loading={copying} onClick={() => void copyToken()}>
            复制令牌
          </Button>
          <Button disabled={busy || !settings?.editable} onClick={() => void saveToken()}>
            更新令牌
          </Button>
        </Space>
      </Card>
      <Card className="ui-management-card" title="Sentry 地址">
        <Space wrap size={4}>
          <Typography.Text code>{sentryOrigin}</Typography.Text>
          <Button aria-label="复制地址" title="复制地址" variant="ghost" size="xs" icon={<IconCopy />}
            onClick={() => void copyAddress()} />
        </Space>
      </Card>
    </section>
  );
}
