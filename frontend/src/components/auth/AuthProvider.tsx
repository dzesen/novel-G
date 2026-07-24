"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { apiGet, apiPost, configureApiAuth } from "@/lib/api";
import {
  migrateLegacyStorageForInitialAdmin,
  setCurrentStorageUser,
} from "@/lib/userStorage";


export interface AuthUser {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "user";
  status: "active" | "disabled";
}

interface AuthPayload {
  user: AuthUser;
  csrf_token: string;
}

type AuthPhase = "loading" | "setup" | "unauthenticated" | "authenticated";

interface AuthContextValue {
  phase: AuthPhase;
  user: AuthUser | null;
  login: (username: string, password: string) => Promise<AuthUser>;
  setup: (
    username: string,
    displayName: string,
    password: string,
  ) => Promise<AuthUser>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);


export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<AuthPhase>("loading");
  const [user, setUser] = useState<AuthUser | null>(null);
  const csrfRef = useRef<string | null>(null);

  const clearSession = useCallback(() => {
    csrfRef.current = null;
    setCurrentStorageUser(null);
    setUser(null);
    setPhase("unauthenticated");
  }, []);

  const applySession = useCallback((payload: AuthPayload) => {
    csrfRef.current = payload.csrf_token;
    setCurrentStorageUser(payload.user.id);
    if (payload.user.role === "admin") {
      migrateLegacyStorageForInitialAdmin();
    }
    setUser(payload.user);
    setPhase("authenticated");
    return payload.user;
  }, []);

  const refresh = useCallback(async () => {
    setPhase("loading");
    try {
      const status = await apiGet<{ setup_required: boolean }>("/api/auth/setup-status");
      if (status.setup_required) {
        csrfRef.current = null;
        setCurrentStorageUser(null);
        setUser(null);
        setPhase("setup");
        return;
      }
      const payload = await apiGet<AuthPayload>("/api/auth/me");
      applySession(payload);
    } catch {
      clearSession();
    }
  }, [applySession, clearSession]);

  useEffect(() => {
    configureApiAuth({
      getCsrfToken: () => csrfRef.current,
      onUnauthorized: clearSession,
    });
    void refresh();
    return () => {
      configureApiAuth({
        getCsrfToken: () => null,
        onUnauthorized: () => {},
      });
    };
  }, [clearSession, refresh]);

  const login = useCallback(
    async (username: string, password: string) => {
      const payload = await apiPost<AuthPayload>("/api/auth/login", {
        username,
        password,
      });
      return applySession(payload);
    },
    [applySession],
  );

  const setup = useCallback(
    async (username: string, displayName: string, password: string) => {
      const payload = await apiPost<AuthPayload>("/api/auth/setup", {
        username,
        display_name: displayName,
        password,
      });
      return applySession(payload);
    },
    [applySession],
  );

  const logout = useCallback(async () => {
    try {
      await apiPost<void>("/api/auth/logout", {});
    } finally {
      clearSession();
    }
  }, [clearSession]);

  const value = useMemo(
    () => ({ phase, user, login, setup, logout, refresh }),
    [phase, user, login, setup, logout, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}


export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
