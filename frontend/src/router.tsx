import { createBrowserRouter } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";
import { AccountPage } from "./features/auth/AccountPage";
import { AdminPage } from "./features/auth/AdminPage";
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
      { path: "account", element: <ProtectedRoute><AccountPage /></ProtectedRoute> },
      { path: "admin", element: <ProtectedRoute admin><AdminPage /></ProtectedRoute> },
    ],
  },
  {
    path: "/login",
    element: <LoginPage />,
  },
]);
