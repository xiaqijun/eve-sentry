import { FormEvent, useState } from "react";
import { Alert, Button, Card, Divider, Input, Tag, Typography } from "@arco-design/web-react";
import { LogIn, Orbit, ShieldCheck } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { apiPath } from "./api";
import { useAuth } from "./AuthContext";

const ESI_ERRORS: Record<string, string> = {
  eve_corporation_not_allowed: "该 EVE 角色不在允许登录的军团中",
  esi_login_unavailable: "EVE 登录尚未配置",
  eve_character_not_assigned: "该 EVE 角色尚未绑定平台账号",
  eve_character_ambiguous: "该 EVE 角色绑定了多个平台账号，请联系管理员",
  user_disabled: "账号已被禁用",
  invalid_esi_state: "登录请求已失效，请重新登录",
  expired_esi_state: "登录请求已过期，请重新登录",
  identity_validation_unavailable: "EVE 身份服务暂时不可用",
};

export function LoginPage() {
  const { authEnabled, loading, login, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [loginError, setLoginError] = useState("");
  const queryErrorCode = new URLSearchParams(location.search).get("esi_error") || "";
  const queryError = queryErrorCode ? ESI_ERRORS[queryErrorCode] || "EVE 登录失败" : "";
  const error = loginError || queryError;

  if (!loading && (!authEnabled || user)) {
    return <Navigate replace to="/" />;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setLoginError("");
    if (queryErrorCode) {
      const searchParams = new URLSearchParams(location.search);
      searchParams.delete("esi_error");
      const search = searchParams.toString();
      navigate(
        { pathname: location.pathname, search: search ? `?${search}` : "", hash: location.hash },
        { replace: true, state: location.state },
      );
    }
    try {
      await login(username, password);
      const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(from || "/", { replace: true });
    } catch (reason) {
      setLoginError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
  const esiLoginUrl = apiPath(
    `/api/v1/auth/esi/start?return_to=${encodeURIComponent(from || "/")}`,
  );

  return (
    <main className="auth-page">
      <div className="auth-layout">
        <section className="auth-brand-panel">
          <span className="auth-brand-mark"><ShieldCheck size={24} /></span>
          <div>
            <Typography.Text>EVE Sentry</Typography.Text>
            <Typography.Title heading={2}>预警管理平台</Typography.Title>
          </div>
          <Tag className="auth-brand-status" color="green">情报服务在线</Tag>
        </section>
        <Card className="auth-login-panel">
          <div className="auth-login-heading">
            <Typography.Text type="secondary">身份认证</Typography.Text>
            <Typography.Title heading={4}>进入管理系统</Typography.Title>
          </div>
          <a className="esi-member-login" href={esiLoginUrl}>
            <Orbit size={18} />
            使用 EVE Online 登录
          </a>
          {error ? <div role="alert"><Alert className="auth-error" closable={false} content={error} type="error" /></div> : null}
          <Divider className="auth-login-divider">管理员</Divider>
          <form className="admin-login-form" onSubmit={submit}>
            <label>
              <span>用户名</span>
              <Input autoComplete="username" required value={username} onChange={setUsername} />
            </label>
            <label>
              <span>密码</span>
              <Input.Password autoComplete="current-password" required value={password} onChange={setPassword} />
            </label>
            <Button htmlType="submit" icon={<LogIn size={16} />} loading={submitting} long type="primary" disabled={loading}>
              {submitting ? "正在登录" : "管理员登录"}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
