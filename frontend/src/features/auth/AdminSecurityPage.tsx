import { useEffect, useState } from "react";
import { Alert, Card, Switch, Typography } from "@arco-design/web-react";
import { ShieldCheck } from "lucide-react";

import {
  ManagementError,
  ManagementPageHeader,
} from "../../components/ManagementPage";
import { fetchSecuritySettings, updateSecuritySettings } from "./api";

export function AdminSecurityPage() {
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void fetchSecuritySettings()
      .then((settings) => {
        if (active) setEnabled(Boolean(settings.key_risk_control));
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "安全设置加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const changeRiskControl = async (next: boolean) => {
    const previous = enabled;
    setEnabled(next);
    setSaving(true);
    setError("");
    try {
      const settings = await updateSecuritySettings({ key_risk_control: next });
      setEnabled(Boolean(settings.key_risk_control));
    } catch (reason) {
      setEnabled(previous);
      setError(reason instanceof Error ? reason.message : "安全设置保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="admin-shell">
      <ManagementPageHeader loading={loading} title="安全设置" />
      <ManagementError error={error} />
      <Card className="arco-management-card security-settings-card">
        <div className="security-setting-row">
          <div className="security-setting-icon"><ShieldCheck size={20} /></div>
          <div className="security-setting-copy">
            <Typography.Title heading={5}>设备密钥风控</Typography.Title>
            <Typography.Paragraph type="secondary">
              开启后，客户端上报的 EVE 角色会通过 ESI、允许军团和角色白名单校验。
              关闭后，有效设备密钥直接可用，身份上报不会访问 ESI。
            </Typography.Paragraph>
          </div>
          <Switch
            aria-label="设备密钥风控"
            checked={enabled}
            loading={loading || saving}
            onChange={(next) => void changeRiskControl(next)}
          />
        </div>
        {!enabled ? (
          <Alert
            className="security-setting-alert"
            content="当前为关闭状态。管理员可以为未登录 ESI 的用户直接创建设备密钥；密钥认证、吊销和账号禁用仍然有效。"
            type="warning"
          />
        ) : null}
      </Card>
    </div>
  );
}
