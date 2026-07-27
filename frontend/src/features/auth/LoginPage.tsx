import { FormEvent, useState } from "react";
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
  const queryError = new URLSearchParams(location.search).get("esi_error") || "";
  const [error, setError] = useState(
    queryError ? ESI_ERRORS[queryError] || "EVE 登录失败" : "",
  );

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
            <p>EVE Sentry</p>
            <h1>预警管理平台</h1>
          </div>
          <span className="auth-brand-status">情报服务在线</span>
        </section>
        <section className="auth-login-panel">
          <div className="auth-login-heading">
            <p>身份认证</p>
            <h2>进入管理系统</h2>
          </div>
          <a className="esi-member-login" href={esiLoginUrl}>
            <Orbit size={18} />
            使用 EVE Online 登录
          </a>
          {error ? <p className="auth-error" role="alert">{error}</p> : null}
          <div className="auth-login-divider"><span>管理员</span></div>
          <form className="admin-login-form" onSubmit={submit}>
            <label>
              <span>用户名</span>
              <input autoComplete="username" required value={username} onChange={(e) => setUsername(e.target.value)} />
            </label>
            <label>
              <span>密码</span>
              <input autoComplete="current-password" required type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </label>
            <button disabled={submitting || loading} type="submit">
              <LogIn size={16} />
              {submitting ? "正在登录" : "管理员登录"}
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}
