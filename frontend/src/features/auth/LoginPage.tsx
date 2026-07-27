import { FormEvent, useState } from "react";
import { LockKeyhole, LogIn } from "lucide-react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "./AuthContext";

export function LoginPage() {
  const { authEnabled, loading, login, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  if (!loading && (!authEnabled || user)) {
    return <Navigate replace to="/" />;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await login(username, password);
      const from = (location.state as { from?: { pathname?: string } } | null)?.from?.pathname;
      navigate(from || "/", { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-page">
      <form className="auth-login-panel" onSubmit={submit}>
        <div className="auth-mark"><LockKeyhole size={22} /></div>
        <p className="eyebrow">EVE 哨兵</p>
        <h1>登录情报平台</h1>
        <label>
          <span>用户名</span>
          <input autoComplete="username" required value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label>
          <span>密码</span>
          <input autoComplete="current-password" required type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error ? <p className="auth-error" role="alert">{error}</p> : null}
        <button disabled={submitting || loading} type="submit">
          <LogIn size={16} />
          {submitting ? "正在登录" : "登录"}
        </button>
      </form>
    </main>
  );
}
