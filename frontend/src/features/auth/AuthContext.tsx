import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  type Annotator,
  type LoginResult,
  logout as apiLogout,
  resolveSession,
  signInWithGoogle,
} from "./authApi";

interface AuthState {
  annotator: Annotator | null;
  loading: boolean;
  signIn: (credential: string, staySignedIn?: boolean) => Promise<LoginResult>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthCtx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [annotator, setAnnotator] = useState<Annotator | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    // Resolves a URL token hand-off (?token=…&refreshToken=…) or a stored
    // cookie session, validating against the backend before trusting it.
    resolveSession().then((a) => {
      if (!alive) return;
      setAnnotator(a);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
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

  const refresh = useCallback(async () => {
    setAnnotator(await resolveSession());
  }, []);

  return <AuthCtx.Provider value={{ annotator, loading, signIn, signOut, refresh }}>{children}</AuthCtx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
