import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { ApiError, fetchMe, login as loginRequest, logout as logoutRequest } from "./api";
import type { AuthUser } from "./types";

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  authEnabled: boolean;
  login: (username: string, password: string) => Promise<AuthUser>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(true);

  const refresh = async () => {
    try {
      setUser(await fetchMe());
      setAuthEnabled(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAuthEnabled(true);
        setUser(null);
        return;
      }
      if (error instanceof ApiError && error.status === 404) {
        setAuthEnabled(false);
        setUser(null);
        return;
      }
      throw error;
    }
  };

  useEffect(() => {
    void refresh().catch(() => setUser(null)).finally(() => setLoading(false));
    const clear = () => setUser(null);
    window.addEventListener("eve-sentry-auth-required", clear);
    return () => window.removeEventListener("eve-sentry-auth-required", clear);
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    user,
    loading,
    authEnabled,
    login: async (username, password) => {
      const authenticated = await loginRequest(username, password);
      setAuthEnabled(true);
      setUser(authenticated);
      return authenticated;
    },
    logout: async () => {
      await logoutRequest();
      setUser(null);
    },
    refresh,
  }), [authEnabled, loading, user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
