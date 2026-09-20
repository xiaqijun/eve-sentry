import { useEffect, useRef, useState } from "react";
import { Breadcrumb, Button, Drawer, Layout, Menu, Tag } from "@arco-design/web-react";
import {
  IconApps,
  IconBook,
  IconDashboard,
  IconDesktop,
  IconCloud,
  IconFile,
  IconHistory,
  IconIdcard,
  IconLock,
  IconSafe,
  IconSettings,
  IconUserGroup,
  IconMenu,
} from "@arco-design/web-react/icon";
import { ShieldCheck } from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { AccountMenu } from "./AccountMenu";
import { ThemeToggle } from "./ThemeToggle";
import { useTheme } from "./ThemeContext";

const PAGE_META: Record<string, { title: string }> = {
  "/": { title: "星图态势" },
  "/dashboard": { title: "工作台" },
  "/reports": { title: "来袭分析" },
  "/reports/history": { title: "来袭历史" },
  "/account/keys": { title: "设备密钥" },
  "/admin/users": { title: "用户管理" },
  "/admin/clients": { title: "客户端管理" },
  "/admin/identity": { title: "身份记录" },
  "/admin/security": { title: "安全设置" },
  "/admin/whitelist": { title: "白名单管理" },
  "/admin/audit": { title: "审计日志" },
  "/admin/esi-gateway": { title: "ESI 网关观测" },
};

const { Header, Content, Sider } = Layout;
const { Item: MenuItem, ItemGroup: MenuItemGroup } = Menu;

function navigationClass({ isActive }: { isActive: boolean }) {
  return isActive ? "management-nav-link active" : "management-nav-link";
}

export function ManagementShell() {
  const { authEnabled, user } = useAuth();
  const { theme } = useTheme();
  const location = useLocation();
  const page = PAGE_META[location.pathname] || PAGE_META["/"];
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
          <span className="management-brand-mark"><ShieldCheck size={20} /></span>
          <div>
            <strong>EVE Sentry</strong>
          </div>
        </div>

        <Menu
          aria-label="主导航"
          className="management-navigation arco-management-menu"
          selectedKeys={[location.pathname]}
          theme={theme}
        >
          <MenuItemGroup key="monitor" title="监控与情报">
            <MenuItem key="/dashboard">
              <NavLink className={navigationClass} to="/dashboard"><IconDashboard /><span>工作台</span></NavLink>
            </MenuItem>
            <MenuItem key="/">
              <NavLink className={navigationClass} end to="/"><IconApps /><span>星图态势</span></NavLink>
            </MenuItem>
            <MenuItem key="/reports">
              <NavLink className={navigationClass} end to="/reports"><IconBook /><span>来袭分析</span></NavLink>
            </MenuItem>
            <MenuItem key="/reports/history">
              <NavLink className={navigationClass} to="/reports/history"><IconHistory /><span>来袭历史</span></NavLink>
            </MenuItem>
          </MenuItemGroup>
          {user ? (
            <MenuItemGroup key="account" title="个人账号">
              <MenuItem key="/account/keys">
                <NavLink className={navigationClass} to="/account/keys"><IconLock /><span>设备密钥</span></NavLink>
              </MenuItem>
            </MenuItemGroup>
          ) : null}
          {user?.role === "admin" ? (
            <MenuItemGroup key="admin" title="系统管理">
              <MenuItem key="/admin/users">
                <NavLink className={navigationClass} to="/admin/users"><IconUserGroup /><span>用户管理</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/clients">
                <NavLink className={navigationClass} to="/admin/clients"><IconDesktop /><span>客户端管理</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/identity">
                <NavLink className={navigationClass} to="/admin/identity"><IconIdcard /><span>身份记录</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/security">
                <NavLink className={navigationClass} to="/admin/security"><IconSettings /><span>安全设置</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/whitelist">
                <NavLink className={navigationClass} to="/admin/whitelist"><IconSafe /><span>白名单管理</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/audit">
                <NavLink className={navigationClass} to="/admin/audit"><IconFile /><span>审计日志</span></NavLink>
              </MenuItem>
              <MenuItem key="/admin/esi-gateway">
                <NavLink className={navigationClass} to="/admin/esi-gateway"><IconCloud /><span>ESI 网关观测</span></NavLink>
              </MenuItem>
            </MenuItemGroup>
          ) : null}
        </Menu>
    </>
  );

  return (
    <Layout className="management-shell arco-management-shell">
      <a className="management-skip-link" href="#management-content">跳到页面内容</a>
      <Sider className="management-sidebar arco-management-sider" width={236}>
        {navigation}
      </Sider>
      <Drawer
        className="management-navigation-drawer"
        title="页面导航"
        placement="left"
        width="min(320px, 88vw)"
        visible={navigationOpen}
        footer={null}
        unmountOnExit
        focusLock
        autoFocus
        escToExit
        onCancel={() => setNavigationOpen(false)}
        afterClose={() => navigationTrigger.current?.focus()}
      >
        <div id="mobile-navigation" role="dialog" aria-label="页面导航" aria-modal="true" onClick={(event) => {
          if ((event.target as HTMLElement).closest("a")) setNavigationOpen(false);
        }}>{navigation}</div>
      </Drawer>
      <Layout className="management-main">
        <Header className="management-topbar arco-management-header">
          <Button
            ref={navigationTrigger}
            className="management-navigation-trigger"
            aria-label="打开导航菜单"
            aria-controls="mobile-navigation"
            aria-expanded={navigationOpen}
            icon={<IconMenu />}
            onClick={() => setNavigationOpen(true)}
          />
          <div className="management-page-context">
            <Breadcrumb className="management-breadcrumb">
              <Breadcrumb.Item>EVE Sentry</Breadcrumb.Item>
              <Breadcrumb.Item>{page.title}</Breadcrumb.Item>
            </Breadcrumb>
            {location.pathname === "/" ? <h1 className="management-map-title">{page.title}</h1> : null}
          </div>
          <div className="management-topbar-actions">
            <ThemeToggle />
            <Tag className="management-live-status" color="gray">
              {authEnabled ? "登录模式" : "公开模式"}
            </Tag>
            <AccountMenu />
          </div>
        </Header>
        <Content id="management-content" role="main" tabIndex={-1} className={`management-content${location.pathname === "/" ? " management-content-workbench" : ""}`}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
