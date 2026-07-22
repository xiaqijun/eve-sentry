import { createBrowserRouter } from "react-router-dom";

import { HostileReportPage } from "./features/reports/HostileReportPage";
import { WorkbenchPage } from "./features/workbench/WorkbenchPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <WorkbenchPage />,
  },
  {
    path: "/reports",
    element: <HostileReportPage />,
  },
]);
