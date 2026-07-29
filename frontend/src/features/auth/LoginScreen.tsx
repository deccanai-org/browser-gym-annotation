import { useState } from "react";
import { useAuth } from "./AuthContext";
import { GoogleAuthButton } from "./GoogleAuthButton";

const C = {
  bg: "#f4f6fa", card: "#ffffff", ink: "#1a2233", muted: "#5c6676", faint: "#8b94a3",
  border: "#e2e7ee", primary: "#4f46e5", danger: "#c02b1d", chip: "#f2f4f8",
};

export function LoginScreen() {
  const { signIn } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [staySignedIn, setStaySignedIn] = useState(true);

  const handleCredential = async (credential: string) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    const r = await signIn(credential, staySignedIn);
    // On success the app re-renders to the platform; only reset on failure.
    if (!r.ok) {
      setError(r.error);
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", background: C.bg, display: "flex", alignItems: "center", justifyContent: "center", padding: 24, fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" }}>
      <div style={{ width: "100%", maxWidth: 400 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18, justifyContent: "center" }}>
          <span style={{ width: 34, height: 34, borderRadius: 9, background: C.primary, color: "#fff", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 18 }}>◆</span>
          <span style={{ fontWeight: 700, fontSize: 17, color: C.ink }}>Browser-Use Gym · Annotator</span>
        </div>

        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 14, padding: "28px 24px", boxShadow: "0 1px 3px rgba(20,30,50,.05)" }}>
          <h1 style={{ fontSize: 19, margin: "0 0 4px", color: C.ink }}>Sign in</h1>
          <p style={{ margin: "0 0 20px", color: C.muted, fontSize: 13.5 }}>Continue with your Google account to access the platform.</p>

          {error && (
            <div role="alert" style={{ background: "#fdece9", color: C.danger, borderRadius: 8, padding: "8px 11px", fontSize: 13, marginBottom: 16 }}>
              {error}
            </div>
          )}

          <div style={{ display: "flex", justifyContent: "center", minHeight: 44, opacity: busy ? 0.6 : 1, pointerEvents: busy ? "none" : "auto" }}>
            <GoogleAuthButton
              onCredential={handleCredential}
              onError={() => setError("Google sign-in failed. Please try again.")}
            />
          </div>

          {busy && <div style={{ textAlign: "center", color: C.faint, fontSize: 12.5, marginTop: 12 }}>Signing in…</div>}

          <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 18, color: C.muted, fontSize: 13, cursor: "pointer" }}>
            <input type="checkbox" checked={staySignedIn} onChange={(e) => setStaySignedIn(e.target.checked)} />
            Keep me signed in
          </label>
        </div>

        <p style={{ textAlign: "center", color: C.faint, fontSize: 12, marginTop: 16 }}>
          Sign-in is restricted to authorized annotator accounts.
        </p>
      </div>
    </div>
  );
}
