import { FormEvent, useState } from "react";
import { Button, Card, Input, Typography } from "@arco-design/web-react";
import { LogIn, ShieldCheck } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { showOperationError } from "../../components/operationFeedback";
import { useAuth } from "./AuthContext";
import { ThemeToggle } from "../shell/ThemeToggle";

export function LoginPage() {
  const { authEnabled, loading, login, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (!loading && (!authEnabled || user)) {
    return <Navigate replace to="/" />;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    try {
      await login(username, password);
      const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(from || "/", { replace: true });
    } catch (reason) {
      showOperationError(reason, "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-page">
      <ThemeToggle className="login-theme-toggle" />
      <div className="auth-layout">
        <div aria-hidden="true" className="auth-brand-panel" />
        <Card className="auth-login-panel">
          <header className="auth-login-heading">
            <span className="auth-brand-mark"><ShieldCheck aria-hidden="true" size={24} /></span>
            <Typography.Title heading={1}>EVE Sentry</Typography.Title>
            <Typography.Text type="secondary">平台账号</Typography.Text>
          </header>
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
