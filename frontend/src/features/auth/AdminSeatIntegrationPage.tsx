import { useCallback, useEffect, useState } from "react";
import { Button, Card, Input, Space, Switch, Tag, Typography } from "@arco-design/web-react";

import { ManagementError, ManagementPageHeader } from "../../components/ManagementPage";
import { apiPath, fetchSeatIntegration, saveSeatServiceToken, updateSeatIntegration } from "./api";
import type { SeatIntegrationSettings } from "./types";

export function AdminSeatIntegrationPage() {
  const [settings, setSettings] = useState<SeatIntegrationSettings | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [serviceToken, setServiceToken] = useState("");
  const [generatedToken, setGeneratedToken] = useState("");
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
    setNotice("");
    try {
      const next = await updateSeatIntegration(enabled, settings.revision);
      setSettings(next);
      setEnabled(next.enabled);
      setNotice(next.enabled ? "已切换至 SeAT 版" : "已切换至独立版");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存 SeAT 设置失败");
    } finally {
      setSaving(false);
    }
  }

  async function saveToken(generate: boolean) {
    if (!settings || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await saveSeatServiceToken(settings.revision, generate ? undefined : serviceToken.trim());
      setSettings(result.settings);
      setServiceToken("");
      setGeneratedToken(result.service_token || "");
      setNotice("服务令牌已保存");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存服务令牌失败");
    } finally {
      setSaving(false);
    }
  }

  const busy = loading || saving;
  const canSave = settings?.editable
    && (settings.enabled !== enabled || (!enabled && !settings.local_issuance))
    && (!enabled || settings.token_configured);

  return (
    <section className="content-page account-page arco-management-page">
      <ManagementPageHeader
        title="SeAT 接入"
        loading={loading}
        extra={<Button disabled={busy} onClick={() => void load()}>刷新状态</Button>}
      />
      <ManagementError error={error} />
      {notice ? <div role="status">{notice}</div> : null}
      <Card className="arco-management-card" title="运行模式" loading={loading}>
        {settings ? (
          <Space direction="vertical" size="medium">
            <Space wrap>
              <Tag color={settings.enabled ? "green" : "gray"}>
                {settings.enabled ? "SeAT 版" : settings.local_issuance ? "独立版" : "启动配置"}
              </Tag>
              <Typography.Text>{settings.token_configured ? "服务令牌已配置" : "服务令牌未配置"}</Typography.Text>
            </Space>
            <Space wrap>
              <Typography.Text>启用 SeAT</Typography.Text>
              <Switch aria-label="启用 SeAT" checked={enabled} disabled={busy || !settings.editable}
                onChange={(value) => { setEnabled(value); setNotice(""); }} />
              <Button type="primary" disabled={busy || !canSave} loading={saving} onClick={() => void save()}>
                保存模式
              </Button>
            </Space>
            <Typography.Text type="secondary">
              开启后客户端使用 SeAT 签发的密钥。关闭后在 Sentry 的「设备密钥」签发本地密钥。切换后，另一模式的客户端需换用密钥才能继续连接。
            </Typography.Text>
            {!settings.editable ? <Typography.Text type="secondary">启用强制鉴权后可在此切换。</Typography.Text> : null}
            {enabled && !settings.token_configured
              ? <Typography.Text type="warning">请先生成或保存服务令牌，再启用。</Typography.Text>
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
      <Card className="arco-management-card" title="服务令牌">
        <Space direction="vertical" size="medium">
          <label htmlFor="seat-service-token">输入现有令牌</label>
          <Input.Password id="seat-service-token" autoComplete="new-password" maxLength={512}
            value={serviceToken} disabled={busy || !settings?.editable} onChange={setServiceToken} />
          <Space wrap>
            <Button disabled={busy || !settings?.editable || serviceToken.trim().length < 32}
              onClick={() => void saveToken(false)}>保存令牌</Button>
            <Button disabled={busy || !settings?.editable}
              onClick={() => void saveToken(true)}>生成并保存新令牌</Button>
          </Space>
          {settings?.token_configured ? (
            <Typography.Text type="secondary">保存新令牌后，请同步更新 SeAT 中的令牌。</Typography.Text>
          ) : null}
          {generatedToken ? (
            <div>
              <Typography.Text>新令牌仅在本次保存后显示，请复制到 SeAT。</Typography.Text>
              <Typography.Paragraph code copyable>{generatedToken}</Typography.Paragraph>
              <Button onClick={() => setGeneratedToken("")}>隐藏令牌</Button>
            </div>
          ) : null}
        </Space>
      </Card>
      <Card className="arco-management-card" title="在 SeAT 完成接入">
        <ol>
          <li>在本页生成或保存令牌，然后启用 SeAT 并保存模式。</li>
          <li>
            在 SeAT 的 <code>/sentry</code> 页面打开「Sentry 接入设置」，填入同一个令牌和 SeAT 能访问的 Sentry HTTPS 地址。
            <Typography.Paragraph code copyable>{sentryOrigin}</Typography.Paragraph>
          </li>
          <li>在 SeAT 测试并保存接入，再刷新本页查看最近一次服务鉴权。</li>
        </ol>
      </Card>
    </section>
  );
}
