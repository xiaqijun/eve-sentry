import { createBrowserRouter, Navigate } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";
import { AccountKeysPage } from "./features/auth/AccountKeysPage";
import { AccountSecurityPage } from "./features/auth/AccountSecurityPage";
import { AdminAuditPage } from "./features/auth/AdminAuditPage";
import { AdminIdentityPage } from "./features/auth/AdminIdentityPage";
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
      { path: "reports", element: <HostileReportPage /> },
      { path: "account", element: <Navigate replace to="/account/keys" /> },
      { path: "account/keys", element: <ProtectedRoute><AccountKeysPage /></ProtectedRoute> },
      { path: "account/security", element: <ProtectedRoute admin><AccountSecurityPage /></ProtectedRoute> },
      { path: "admin", element: <Navigate replace to="/admin/users" /> },
      { path: "admin/users", element: <ProtectedRoute admin><AdminUsersPage /></ProtectedRoute> },
      { path: "admin/identity", element: <ProtectedRoute admin><AdminIdentityPage /></ProtectedRoute> },
      { path: "admin/whitelist", element: <ProtectedRoute admin><AdminWhitelistPage /></ProtectedRoute> },
      { path: "admin/audit", element: <ProtectedRoute admin><AdminAuditPage /></ProtectedRoute> },
    ],
  },
  {
    path: "/login",
    element: <LoginPage />,
  },
]);
