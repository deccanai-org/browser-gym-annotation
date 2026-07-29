import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  type Annotator,
  type LoginResult,
  logout as apiLogout,
  restoreSession,
  signInWithGoogle,
} from "./authApi";

interface AuthState {
  annotator: Annotator | null;
  loading: boolean;
  signIn: (credential: string, staySignedIn?: boolean) => Promise<LoginResult>;
  signOut: () => Promise<void>;
  refresh: () => void;
}

const AuthCtx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [annotator, setAnnotator] = useState<Annotator | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setAnnotator(restoreSession());
    setLoading(false);
  }, []);

  const signIn = useCallback(async (credential: string, staySignedIn = false) => {
    const r = await signInWithGoogle(credential, staySignedIn);
    if (r.ok) setAnnotator(r.annotator);
    return r;
  }, []);

  const signOut = useCallback(async () => {
    apiLogout();
    setAnnotator(null);
  }, []);

  const refresh = useCallback(() => {
    setAnnotator(restoreSession());
  }, []);

  return <AuthCtx.Provider value={{ annotator, loading, signIn, signOut, refresh }}>{children}</AuthCtx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
