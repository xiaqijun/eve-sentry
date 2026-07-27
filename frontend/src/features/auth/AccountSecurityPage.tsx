import { FormEvent, useState } from "react";
import { BadgeCheck, LockKeyhole, ShieldCheck, UserRound } from "lucide-react";

import { changePassword } from "./api";
import { useAuth } from "./AuthContext";

export function AccountSecurityPage() {
  const { refresh, user } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const updatePassword = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setMessage("");
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setMessage("密码已更新");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密码更新失败");
    }
  };

  return (
    <div className="account-shell">
      <header className="content-page-header account-header">
        <div>
          <h2>账号安全</h2>
        </div>
        <span className="page-identity-badge"><BadgeCheck size={16} />{user?.display_name || user?.username}</span>
      </header>

      {error ? <div className="auth-banner error" role="alert">{error}</div> : null}
      {message ? <div className="auth-banner">{message}</div> : null}

      <section className="account-summary" aria-label="账号摘要">
        <div><span>显示名称</span><strong>{user?.display_name || user?.username}</strong></div>
        <div><span>账号角色</span><strong>{user?.role === "admin" ? "管理员" : "普通用户"}</strong></div>
        <div><span>账号状态</span><strong>{user?.status === "active" ? "正常" : "已禁用"}</strong></div>
      </section>

      <section className="security-grid">
        <article className="account-panel account-profile-panel">
          <div className="account-panel-heading">
            <div className="account-panel-title"><UserRound size={17} /><h2>账号信息</h2></div>
          </div>
          <dl className="account-definition-list">
            <div><dt>用户名</dt><dd>{user?.username}</dd></div>
            <div><dt>显示名称</dt><dd>{user?.display_name || "未设置"}</dd></div>
            <div><dt>权限角色</dt><dd>{user?.role === "admin" ? "管理员" : "普通用户"}</dd></div>
          </dl>
        </article>

        <article className="account-panel account-password-panel">
          <div className="account-panel-heading">
            <div className="account-panel-title"><ShieldCheck size={17} /><h2>修改登录密码</h2></div>
          </div>
          <form className="stack-form" onSubmit={updatePassword}>
            <label><span>当前密码</span><input autoComplete="current-password" required type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
            <label><span>新密码</span><input autoComplete="new-password" minLength={12} required type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label>
            <button type="submit"><LockKeyhole size={15} />更新密码</button>
          </form>
        </article>
      </section>
    </div>
  );
}
