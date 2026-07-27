import {
  BarChart3,
  KeyRound,
  LayoutDashboard,
  LogOut,
  Radio,
  ShieldCheck,
  UsersRound,
} from "lucide-react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

const PAGE_META: Record<string, { title: string; description: string }> = {
  "/": { title: "态势工作台", description: "实时监控节点、敌对目标与告警" },
  "/reports": { title: "来袭报表", description: "敌对来袭趋势与历史记录" },
  "/account": { title: "账号与密钥", description: "个人密码与客户端访问密钥" },
  "/admin": { title: "用户与权限", description: "用户、EVE 身份和授权策略" },
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
            <small>预警管理平台</small>
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

          {user ? <span className="management-nav-section">系统管理</span> : null}
          {user ? (
            <NavLink className={navigationClass} to="/account">
              <KeyRound size={17} />
              <span>账号与密钥</span>
            </NavLink>
          ) : null}
          {user?.role === "admin" ? (
            <NavLink className={navigationClass} to="/admin">
              <UsersRound size={17} />
              <span>用户与权限</span>
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
          <div>
            <h1>{page.title}</h1>
            <p>{page.description}</p>
          </div>
          <span className="management-live-status">
            <Radio size={15} />
            {authEnabled ? "服务在线" : "公开模式"}
          </span>
        </header>
        <div className={`management-content${location.pathname === "/" ? " management-content-workbench" : ""}`}>
          <Outlet />
        </div>
      </section>
    </div>
  );
}
