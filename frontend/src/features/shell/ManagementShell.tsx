import {
  BarChart3,
  Building2,
  ChevronRight,
  Fingerprint,
  KeyRound,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Radio,
  ScrollText,
  ShieldCheck,
  UsersRound,
} from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

const PAGE_META: Record<string, { title: string }> = {
  "/": { title: "态势工作台" },
  "/reports": { title: "来袭报表" },
  "/account/keys": { title: "设备密钥" },
  "/account/security": { title: "账号安全" },
  "/admin/users": { title: "用户管理" },
  "/admin/identity": { title: "身份记录" },
  "/admin/whitelist": { title: "白名单管理" },
  "/admin/audit": { title: "审计日志" },
};

function navigationClass({ isActive }: { isActive: boolean }) {
  return isActive ? "management-nav-link active" : "management-nav-link";
}

export function ManagementShell() {
  const { authEnabled, logout, user } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const page = PAGE_META[location.pathname] || PAGE_META["/"];

  const signOut = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="management-shell">
      <aside className="management-sidebar">
        <div className="management-brand">
          <span className="management-brand-mark"><ShieldCheck size={20} /></span>
          <div>
            <strong>EVE Sentry</strong>
          </div>
        </div>

        <nav className="management-navigation" aria-label="主导航">
          <span className="management-nav-section">监控与情报</span>
          <NavLink className={navigationClass} end to="/">
            <LayoutDashboard size={17} />
            <span>态势工作台</span>
          </NavLink>
          <NavLink className={navigationClass} to="/reports">
            <BarChart3 size={17} />
            <span>来袭报表</span>
          </NavLink>

          {user ? <span className="management-nav-section">个人账号</span> : null}
          {user ? (
            <NavLink className={navigationClass} to="/account/keys">
              <KeyRound size={17} />
              <span>设备密钥</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/account/security">
              <LockKeyhole size={17} />
              <span>账号安全</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? <span className="management-nav-section">系统管理</span> : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/admin/users">
              <UsersRound size={17} />
              <span>用户管理</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/admin/identity">
              <Fingerprint size={17} />
              <span>身份记录</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/admin/whitelist">
              <Building2 size={17} />
              <span>白名单管理</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/admin/audit">
              <ScrollText size={17} />
              <span>审计日志</span>
            </NavLink>
          ) : null}
        </nav>

        <div className="management-user-card">
          <span className="management-user-avatar">
            {(user?.display_name || user?.username || "访").slice(0, 1).toUpperCase()}
          </span>
          <div>
            <strong>{user?.display_name || user?.username || "访客模式"}</strong>
            <small>{user ? (user.role === "admin" ? "管理员" : "普通用户") : "认证未启用"}</small>
          </div>
          {user ? (
            <button aria-label="退出登录" title="退出登录" type="button" onClick={() => void signOut()}>
              <LogOut size={16} />
            </button>
          ) : null}
        </div>
      </aside>

      <section className="management-main">
        <header className="management-topbar">
          <div className="management-page-context">
            <div className="management-breadcrumb">
              <span>EVE Sentry</span>
              <ChevronRight size={13} />
              <strong>{page.title}</strong>
            </div>
            <h1>{page.title}</h1>
          </div>
          <div className="management-topbar-actions">
            <span className="management-live-status">
              <Radio size={15} />
              {authEnabled ? "服务在线" : "公开模式"}
            </span>
            <span className="management-topbar-user">
              <b>{(user?.display_name || user?.username || "访").slice(0, 1).toUpperCase()}</b>
              <span>
                <strong>{user?.display_name || user?.username || "访客"}</strong>
                <small>{user ? (user.role === "admin" ? "管理员" : "普通用户") : "无需认证"}</small>
              </span>
            </span>
          </div>
        </header>
        <div className={`management-content${location.pathname === "/" ? " management-content-workbench" : ""}`}>
          <Outlet />
        </div>
      </section>
    </div>
  );
}
