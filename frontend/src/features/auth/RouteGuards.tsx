import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "./AuthContext";

export function ProtectedRoute({
  children,
  admin = false,
}: {
  children: React.ReactNode;
  admin?: boolean;
}) {
  const { loading, user } = useAuth();
  const location = useLocation();
  if (loading) {
    return <main className="auth-loading">正在验证会话...</main>;
  }
  if (!user) {
    return <Navigate replace state={{ from: location }} to="/login" />;
  }
  if (admin && user.role !== "admin") {
    return <Navigate replace to="/account" />;
  }
  return <>{children}</>;
}
