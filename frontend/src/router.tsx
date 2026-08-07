import { createBrowserRouter, Navigate } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
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

export const router = createBrowserRouter([
  {
    path: "/",
    element: <ProtectedRoute allowWhenAuthDisabled><ManagementShell /></ProtectedRoute>,
    children: [
      { index: true, element: <WorkbenchPage /> },
      { path: "dashboard", element: <DashboardPage /> },
      { path: "reports", element: <HostileReportPage /> },
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
  },
]);
