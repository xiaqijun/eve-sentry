import { Button, Card, Space, Typography } from "@arco-design/web-react";
import { createBrowserRouter, isRouteErrorResponse, Navigate, useRouteError } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
import { HostileHistoryPage } from "./features/reports/HostileHistoryPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";
import { AccountKeysPage } from "./features/auth/AccountKeysPage";
import { AdminAuditPage } from "./features/auth/AdminAuditPage";
import { AdminClientsPage } from "./features/auth/AdminClientsPage";
import { AdminIdentityPage } from "./features/auth/AdminIdentityPage";
import { AdminSecurityPage } from "./features/auth/AdminSecurityPage";
import { AdminUsersPage } from "./features/auth/AdminUsersPage";
import { AdminWhitelistPage } from "./features/auth/AdminWhitelistPage";
import { LoginPage } from "./features/auth/LoginPage";
import { ProtectedRoute } from "./features/auth/RouteGuards";
import { ManagementShell } from "./features/shell/ManagementShell";

function RouteErrorPage() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error ? error.message : "页面加载失败";
  return (
    <main className="route-error-page">
      <Card title="页面暂时无法显示">
        <Space direction="vertical" size="medium">
          <Typography.Text type="secondary">{message}</Typography.Text>
          <Typography.Text type="secondary">错误编号：{String(Date.now()).slice(-6)}</Typography.Text>
          <Space>
            <Button type="primary" onClick={() => window.location.reload()}>重新加载</Button>
            <Button onClick={() => { window.location.assign("/"); }}>返回工作台</Button>
          </Space>
        </Space>
      </Card>
    </main>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    element: <ProtectedRoute allowWhenAuthDisabled><ManagementShell /></ProtectedRoute>,
    errorElement: <RouteErrorPage />,
    children: [
      { index: true, element: <WorkbenchPage /> },
      { path: "dashboard", element: <DashboardPage /> },
      { path: "reports", element: <HostileReportPage /> },
      { path: "reports/history", element: <HostileHistoryPage /> },
      { path: "account", element: <Navigate replace to="/account/keys" /> },
      { path: "account/keys", element: <ProtectedRoute><AccountKeysPage /></ProtectedRoute> },
      { path: "account/security", element: <Navigate replace to="/account/keys" /> },
      { path: "admin", element: <Navigate replace to="/admin/users" /> },
      { path: "admin/users", element: <ProtectedRoute admin><AdminUsersPage /></ProtectedRoute> },
      { path: "admin/clients", element: <ProtectedRoute admin><AdminClientsPage /></ProtectedRoute> },
      { path: "admin/identity", element: <ProtectedRoute admin><AdminIdentityPage /></ProtectedRoute> },
      { path: "admin/security", element: <ProtectedRoute admin><AdminSecurityPage /></ProtectedRoute> },
      { path: "admin/whitelist", element: <ProtectedRoute admin><AdminWhitelistPage /></ProtectedRoute> },
      { path: "admin/audit", element: <ProtectedRoute admin><AdminAuditPage /></ProtectedRoute> },
    ],
  },
  {
    path: "/login",
    element: <LoginPage />,
    errorElement: <RouteErrorPage />,
  },
]);
