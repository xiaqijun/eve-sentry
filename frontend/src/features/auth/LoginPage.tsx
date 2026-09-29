import { FormEvent, useState } from "react";
import { Alert, Button, Card, Divider, Input, Tag, Typography } from "@arco-design/web-react";
import { LogIn, ShieldCheck } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "./AuthContext";
import { ThemeToggle } from "../shell/ThemeToggle";

export function LoginPage() {
  const { authEnabled, loading, login, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [loginError, setLoginError] = useState("");
  const error = loginError;

  if (!loading && (!authEnabled || user)) {
    return <Navigate replace to="/" />;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setLoginError("");
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

  return (
    <main className="auth-page">
      <ThemeToggle className="login-theme-toggle" />
      <div className="auth-layout">
        <section className="auth-brand-panel">
          <span className="auth-brand-mark"><ShieldCheck size={24} /></span>
          <div>
            <Typography.Text className="auth-brand-eyebrow">EVE Sentry</Typography.Text>
            <Typography.Title className="auth-brand-title" heading={2}>预警管理平台</Typography.Title>
          </div>
          <Tag className="auth-brand-status" color="green">情报服务在线</Tag>
        </section>
        <Card className="auth-login-panel">
          <div className="auth-login-heading">
            <Typography.Text type="secondary">身份认证</Typography.Text>
            <Typography.Title heading={4}>进入管理系统</Typography.Title>
          </div>
          {error ? <div role="alert"><Alert className="auth-error" closable={false} content={error} type="error" /></div> : null}
          <Divider className="auth-login-divider">平台账号</Divider>
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
              {submitting ? "正在登录" : "登录"}
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
