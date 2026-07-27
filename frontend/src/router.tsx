import { createBrowserRouter } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";
import { AccountPage } from "./features/auth/AccountPage";
import { AdminPage } from "./features/auth/AdminPage";
import { LoginPage } from "./features/auth/LoginPage";
import { ProtectedRoute } from "./features/auth/RouteGuards";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <ProtectedRoute><WorkbenchPage /></ProtectedRoute>,
  },
  {
    path: "/reports",
    element: <ProtectedRoute><HostileReportPage /></ProtectedRoute>,
  },
  {
    path: "/login",
    element: <LoginPage />,
  },
  {
    path: "/account",
    element: <ProtectedRoute><AccountPage /></ProtectedRoute>,
  },
  {
    path: "/admin",
    element: <ProtectedRoute admin><AdminPage /></ProtectedRoute>,
  },
]);
