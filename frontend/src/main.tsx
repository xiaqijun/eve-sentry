import React from "react";
import ReactDOM from "react-dom/client";
import { ToastProvider } from "@/components/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { router } from "./router";
import { AuthProvider } from "./features/auth/AuthContext";
import { ThemeProvider } from "./features/shell/ThemeContext";
import "./index.css";
import "./components/ui/ui.css";
import "./styles.css";
import "./console-usability.css";
import "./console-theme.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <ToastProvider>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <RouterProvider router={router} />
          </AuthProvider>
        </QueryClientProvider>
      </ToastProvider>
    </ThemeProvider>
  </React.StrictMode>,
);
