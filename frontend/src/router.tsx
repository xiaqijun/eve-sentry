import { createBrowserRouter } from "react-router-dom";

import { WorkbenchPage } from "./features/workbench/WorkbenchPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <WorkbenchPage />,
  },
]);
