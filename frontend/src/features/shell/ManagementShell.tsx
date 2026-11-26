import { useEffect, useRef, useState } from "react";
import { Button, Drawer, Tag } from "@/components/ui";
import {
  Cloud, KeyRound, LayoutDashboard, Link2, Menu, Monitor, Orbit, ShieldCheck, Users,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { AccountMenu } from "./AccountMenu";
import { ThemeToggle } from "./ThemeToggle";

function navigationClass({ isActive }: { isActive: boolean }) {
  return isActive ? "management-nav-link active" : "management-nav-link";
}

export function ManagementShell() {
  const { authEnabled, user } = useAuth();
  const location = useLocation();
  const isStarMap = location.pathname === "/";
  const [navigationOpen, setNavigationOpen] = useState(false);
  const navigationTrigger = useRef<HTMLButtonElement>(null);

  useEffect(() => setNavigationOpen(false), [location.pathname, location.search]);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1001px)");
    const closeOnDesktop = () => { if (desktop.matches) setNavigationOpen(false); };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  const navigation = (
    <>
      <div className="management-brand">
        <span className="management-brand-mark"><ShieldCheck aria-hidden="true" size={22} /></span>
        <strong>EVE Sentry</strong>
      </div>
      <nav aria-label="主导航" className="management-navigation">
        <div className="management-nav-group" role="group" aria-label="监控与情报">
          <span aria-hidden="true">监控与情报</span>
          <NavLink className={navigationClass} to="/dashboard"><LayoutDashboard aria-hidden="true" /><span>工作台</span></NavLink>
          <NavLink className={navigationClass} end to="/"><Orbit aria-hidden="true" /><span>星图态势</span></NavLink>
        </div>
        {user ? (
          <div className="management-nav-group" role="group" aria-label="个人账号">
            <span aria-hidden="true">个人账号</span>
            <NavLink className={navigationClass} to="/account/keys"><KeyRound aria-hidden="true" /><span>设备密钥</span></NavLink>
          </div>
        ) : null}
        {user?.role === "admin" ? (
          <div className="management-nav-group" role="group" aria-label="系统管理">
            <span aria-hidden="true">系统管理</span>
            <NavLink className={navigationClass} to="/admin/users"><Users aria-hidden="true" /><span>用户管理</span></NavLink>
            <NavLink className={navigationClass} to="/admin/clients"><Monitor aria-hidden="true" /><span>客户端管理</span></NavLink>
            <NavLink className={navigationClass} to="/admin/seat-integration"><Link2 aria-hidden="true" /><span>SeAT 接入</span></NavLink>
            <NavLink className={navigationClass} to="/admin/esi-gateway"><Cloud aria-hidden="true" /><span>ESI 网关观测</span></NavLink>
          </div>
        ) : null}
      </nav>
    </>
  );

  return (
    <div className="management-shell ui-management-shell">
      <a className="management-skip-link" href="#management-content">跳到页面内容</a>
      <aside className="management-sidebar ui-management-sider ui-layout-sider">
        {navigation}
      </aside>
      <Drawer
        className="management-navigation-drawer"
        title="页面导航"
        placement="left"
        width="min(320px, 88vw)"
        id="mobile-navigation"
        open={navigationOpen}
        onClose={() => setNavigationOpen(false)}
        restoreFocus={() => navigationTrigger.current?.focus()}
      >
        <div onClick={(event) => {
          if ((event.target as HTMLElement).closest("a")) setNavigationOpen(false);
        }}>{navigation}</div>
      </Drawer>
      <div className="management-main ui-layout">
        <header className="management-topbar ui-management-header">
          <Button
            ref={navigationTrigger}
            className="management-navigation-trigger"
            aria-label="打开导航菜单"
            aria-controls="mobile-navigation"
            aria-expanded={navigationOpen}
            icon={<Menu aria-hidden="true" size={18} />}
            onClick={() => setNavigationOpen(true)}
          />
          {isStarMap ? <h1 className="management-map-title">星图态势</h1> : null}
          <div className="management-topbar-actions">
            <ThemeToggle />
            <Tag className="management-live-status" color="gray">
              {authEnabled ? "登录模式" : "公开模式"}
            </Tag>
            <AccountMenu />
          </div>
        </header>
        <main id="management-content" tabIndex={-1} className={`ui-layout-content management-content${isStarMap ? " management-content-workbench" : ""}`}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
