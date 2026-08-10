import { useEffect, useState } from "react";
import { Alert, Card, Divider, Grid, Space, Switch, Tag, Typography } from "@arco-design/web-react";
import {
  ArrowRight,
  Building2,
  CheckCircle2,
  CircleSlash2,
  History,
  KeyRound,
  ShieldCheck,
  UserRoundCheck,
  UsersRound,
} from "lucide-react";
import { Link } from "react-router-dom";

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

  const statusLabel = loading ? "读取中" : saving ? "保存中" : enabled ? "已启用" : "已关闭";
  const statusColor = loading || saving ? "arcoblue" : enabled ? "green" : "orange";

  return (
    <div className="admin-shell">
      <ManagementPageHeader
        extra={<Tag color={statusColor}>{statusLabel}</Tag>}
        loading={loading}
        title="安全设置"
      />
      <ManagementError error={error} />

      <Grid.Row className="security-settings-layout" gutter={20}>
        <Grid.Col lg={16} xs={24}>
          <Card
            className="arco-management-card security-policy-card"
            title={<Space className="security-card-title"><ShieldCheck size={18} /><span>设备密钥风控</span></Space>}
          >
            <div className="security-policy-control">
              <div className="security-setting-copy">
                <Typography.Title heading={6}>持续校验客户端身份</Typography.Title>
                <Typography.Paragraph type="secondary">
                  启用后，客户端上报角色 ID，服务端通过 ESI 解析身份，并结合军团与角色白名单决定密钥是否可继续使用。
                </Typography.Paragraph>
              </div>
              <div className="security-switch-block">
                <Typography.Text className="security-control-label" type="secondary">策略状态</Typography.Text>
                <div className="security-switch-control">
                  <strong aria-live="polite">{enabled ? "启用" : "关闭"}</strong>
                  <Switch
                    aria-label="设备密钥风控"
                    checked={enabled}
                    loading={loading || saving}
                    onChange={(next) => void changeRiskControl(next)}
                  />
                </div>
              </div>
            </div>

            <Divider />

            <section aria-label="身份校验链路" className="security-check-flow">
              <div className="security-check-step">
                <span><UserRoundCheck size={17} /></span>
                <div><strong>识别角色</strong><small>按角色 ID 上报</small></div>
              </div>
              <div className="security-check-step">
                <span><Building2 size={17} /></span>
                <div><strong>解析身份</strong><small>获取角色与军团</small></div>
              </div>
              <div className="security-check-step">
                <span><ShieldCheck size={17} /></span>
                <div><strong>执行策略</strong><small>匹配允许规则</small></div>
              </div>
            </section>

            {!enabled ? (
              <Alert
                className="security-setting-alert"
                content="当前为关闭状态。管理员可以为未登录 ESI 的用户直接创建设备密钥；密钥认证、吊销和账号禁用仍然有效。"
                type="warning"
              />
            ) : null}
          </Card>
        </Grid.Col>

        <Grid.Col className="security-side-column" lg={8} xs={24}>
          <Card
            className="arco-management-card security-impact-card"
            title={<Space className="security-card-title"><KeyRound size={18} /><span>策略影响</span></Space>}
          >
            <div className="security-impact-list">
              <div className="security-impact-item">
                <CheckCircle2 size={17} />
                <div><strong>密钥认证</strong><small>始终验证密钥有效性</small></div>
              </div>
              <div className="security-impact-item">
                <CheckCircle2 size={17} />
                <div><strong>账号状态</strong><small>禁用和吊销始终生效</small></div>
              </div>
              <div className={`security-impact-item ${enabled ? "is-active" : "is-paused"}`}>
                {enabled ? <CheckCircle2 size={17} /> : <CircleSlash2 size={17} />}
                <div>
                  <strong>身份与白名单</strong>
                  <small>{enabled ? "持续执行校验" : "当前暂停校验"}</small>
                </div>
              </div>
            </div>
          </Card>

          <Card
            className="arco-management-card security-links-card"
            title="相关管理"
          >
            <nav aria-label="安全设置相关管理">
              <Link className="security-management-link" to="/admin/whitelist">
                <span><ShieldCheck size={16} /><span>白名单管理</span></span><ArrowRight size={15} />
              </Link>
              <Link className="security-management-link" to="/admin/users">
                <span><UsersRound size={16} /><span>用户与密钥</span></span><ArrowRight size={15} />
              </Link>
              <Link className="security-management-link" to="/admin/audit">
                <span><History size={16} /><span>审计日志</span></span><ArrowRight size={15} />
              </Link>
            </nav>
          </Card>
        </Grid.Col>
      </Grid.Row>
    </div>
  );
}
