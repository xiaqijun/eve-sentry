import { useCallback, useEffect, useState } from "react";
import { Button, Card, Descriptions, Input, Select, Space, Tag } from "@/components/ui";

import { ManagementError } from "../../components/ManagementPage";
import { useOperationFeedback } from "../../components/operationFeedback";
import { apiRequest } from "./api";

const SETTINGS_PATH = "/api/v1/admin/esi-gateway/settings";

interface GatewaySettings {
  url: string;
  token_configured: boolean;
  revision: string;
  allowed_urls: string[];
  editable: boolean;
}

export function EsiGatewaySettingsPanel({ onSaved }: { onSaved: () => void }) {
  const feedback = useOperationFeedback();
  const [settings, setSettings] = useState<GatewaySettings | null>(null);
  const [editing, setEditing] = useState(false);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saveFailed, setSaveFailed] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setSaveFailed(false);
    try {
      const result = await apiRequest<{ settings: GatewaySettings }>(SETTINGS_PATH);
      setSettings(result.settings);
      setEditing(false);
      setToken("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取网关设置失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function runCheck(save: boolean) {
    if (!settings || busy) return;
    setBusy(true);
    setSaveFailed(false);
    try {
      if (save) {
        const result = await apiRequest<{ settings: GatewaySettings }>(SETTINGS_PATH, {
          method: "POST",
          body: JSON.stringify({ url, revision: settings.revision, ...(token.trim() && { token: token.trim() }) }),
        });
        setSettings(result.settings);
        setEditing(false);
        setToken("");
        feedback.success("网关设置已保存");
        onSaved();
      } else {
        await apiRequest("/api/v1/admin/esi-gateway/test", { method: "POST" });
        feedback.success("连接与鉴权通过");
      }
    } catch (reason) {
      setSaveFailed(save);
      feedback.operationError(reason, save ? "保存网关设置失败" : "验证网关连接失败");
    } finally {
      setBusy(false);
    }
  }

  const canSave = Boolean(url && (token.trim().length >= 32 ||
    (!token.trim() && url === settings?.url && settings.token_configured)));

  return (
    <Card className="ui-management-card" title="ESI 网关接入" loading={loading}>
      <Space direction="vertical" size="medium" style={{ width: "100%" }}>
        <ManagementError error={error} />
        {settings ? (
          editing ? (
            <form onSubmit={(event) => { event.preventDefault(); if (canSave) void runCheck(true); }}>
              <Space direction="vertical" size="medium" style={{ width: "100%" }}>
                <Select label="网关地址" placeholder="选择网关地址" value={url || undefined}
                  options={settings.allowed_urls.map((address) => ({ value: address, label: address }))} onValueChange={setUrl} disabled={busy} />
                <Input.Password aria-label="服务令牌" placeholder="服务令牌" autoComplete="new-password"
                  maxLength={512} value={token} onChange={setToken} disabled={busy} />
                <Space wrap>
                  <Button variant="default" type="submit" loading={busy} disabled={!canSave}>测试并保存</Button>
                  <Button disabled={busy} onClick={() => { setEditing(false); setToken(""); setSaveFailed(false); }}>取消</Button>
                  {saveFailed && <Button disabled={busy} onClick={() => void load()}>重新读取</Button>}
                </Space>
              </Space>
            </form>
          ) : (
            <>
              <Descriptions column={1} data={[
                { label: "网关地址", value: settings.url || "未配置" },
                { label: "服务令牌", value: <Tag color={settings.token_configured ? "green" : "gray"}>
                  {settings.token_configured ? "已配置" : "未配置"}
                </Tag> },
              ]} />
              {settings.editable && (
                <Space wrap>
                  <Button disabled={busy} onClick={() => {
                    setUrl(settings.url);
                    setEditing(true);
                    setError("");
                  }}>编辑设置</Button>
                  <Button loading={busy} disabled={!settings.url || !settings.token_configured}
                    onClick={() => void runCheck(false)}>测试连接</Button>
                </Space>
              )}
            </>
          )
        ) : error && <Button onClick={() => void load()}>重新读取</Button>}
      </Space>
    </Card>
  );
}
