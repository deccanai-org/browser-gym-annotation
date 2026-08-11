import { useState } from "react";
import { t, weight } from "../../ds";
import { useAuth } from "./AuthContext";

// The seeded dummy accounts (dev/testing only). Click one to fill the email; the
// shared dev password is shown below. These are throwaway test fixtures.
const TEST_ACCOUNTS = [
  { email: "ana@deccan.ai", name: "Ana Rivera", role: "reviewer" },
  { email: "ben@deccan.ai", name: "Ben Okafor", role: "annotator" },
  { email: "chloe@deccan.ai", name: "Chloe Tan", role: "annotator" },
  { email: "diego@deccan.ai", name: "Diego Santos", role: "annotator" },
  { email: "ela@deccan.ai", name: "Ela Novak", role: "annotator" },
];
const DEV_PASSWORD = "annotate1";

const C = {
  card: t.n9, ink: t.n0, muted: t.n2, faint: t.n3, border: t.n7,
  primary: t.primary6, primaryInk: t.n9, danger: t.redDark, chip: t.n8,
};

export function LoginScreen() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    const r = await signIn(email.trim(), password);
    setBusy(false);
    if (!r.ok) setError(r.error);
  };

  return (
    // The one hero surface in the app, so it gets the full DS mesh plus the
    // wave field the DS reserves for title slides.
    <div style={{ position: "relative", minHeight: "100vh", background: t.bgWashFull, display: "flex", alignItems: "center", justifyContent: "center", padding: 24, fontFamily: t.fontPrimary }}>
      {/* Masked at the top edge — the band is a crop from the DS master, so
          without the fade its straight upper edge reads as a seam. */}
      <div aria-hidden style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: "46vh", background: `${t.assetWaves} bottom center / cover no-repeat`, WebkitMaskImage: "linear-gradient(to bottom, transparent 0%, #000 45%)", maskImage: "linear-gradient(to bottom, transparent 0%, #000 45%)", pointerEvents: "none" }} />
      <div style={{ position: "relative", width: "100%", maxWidth: 400 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18, justifyContent: "center" }}>
          <img src="/deccan-ai-wordmark.svg" alt="Deccan AI" style={{ height: 22, width: "auto" }} />
          <span style={{ fontFamily: t.fontDisplay, fontWeight: weight.semibold, fontSize: 17, color: C.ink, letterSpacing: t.trackingHeading }}>Browser-Use Gym</span>
        </div>
        <form onSubmit={submit} style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: t.radiusXl, padding: "26px 24px", boxShadow: t.shadowCard }}>
          <h1 style={{ fontFamily: t.fontDisplay, fontSize: 19, margin: "0 0 4px", color: t.textTitle }}>Sign in</h1>
          <p style={{ margin: "0 0 18px", color: C.muted, fontSize: 13.5 }}>Log in to your annotator account.</p>

          <label style={{ display: "block", fontSize: 12.5, fontWeight: 600, color: C.muted, marginBottom: 6 }}>Email</label>
          <input
            type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus autoComplete="username"
            placeholder="you@deccan.ai"
            style={{ width: "100%", padding: "10px 12px", borderRadius: t.radiusLg, border: `1px solid ${C.border}`, fontFamily: t.fontPrimary, fontSize: 14, color: C.ink, outline: "none", marginBottom: 14, boxSizing: "border-box" }}
          />
          <label style={{ display: "block", fontSize: 12.5, fontWeight: 600, color: C.muted, marginBottom: 6 }}>Password</label>
          <input
            type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
            placeholder="••••••••"
            style={{ width: "100%", padding: "10px 12px", borderRadius: t.radiusLg, border: `1px solid ${C.border}`, fontFamily: t.fontPrimary, fontSize: 14, color: C.ink, outline: "none", marginBottom: 16, boxSizing: "border-box" }}
          />
          {error && <div role="alert" style={{ background: t.redLite, color: C.danger, borderRadius: t.radiusMd, padding: "8px 11px", fontSize: 13, marginBottom: 14 }}>{error}</div>}
          <button
            type="submit" disabled={busy || !email || !password}
            style={{ width: "100%", padding: "11px", borderRadius: t.radiusLg, border: "none", background: C.primary, color: C.primaryInk, fontWeight: weight.bold, fontSize: 14.5, opacity: busy || !email || !password ? 0.45 : 1, cursor: busy || !email || !password ? "default" : "pointer", transition: `opacity ${t.transitionUi}` }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: t.radiusXl, padding: "16px 18px", marginTop: 14, boxShadow: t.shadowMd }}>
          <div style={{ fontSize: 11.5, fontWeight: 700, letterSpacing: t.trackingEyebrow, textTransform: "uppercase", color: C.faint, marginBottom: 10 }}>Test accounts · click to fill email</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 7 }}>
            {TEST_ACCOUNTS.map((a) => (
              <button
                key={a.email} type="button" onClick={() => { setEmail(a.email); setError(null); }}
                title={`${a.name} · ${a.role}`}
                style={{ border: `1px solid ${email === a.email ? t.primary6 : C.border}`, background: email === a.email ? t.primary0 : C.chip, color: C.ink, borderRadius: t.radiusPill, padding: "5px 11px", fontSize: 12.5, cursor: "pointer", fontWeight: weight.medium, transition: `background ${t.transitionUi}` }}
              >
                {a.name}{a.role === "reviewer" ? " ★" : ""}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 12.5, color: C.muted, marginTop: 12 }}>
            Password for all test accounts: <code style={{ background: C.chip, fontFamily: t.fontMono, padding: "2px 7px", borderRadius: t.radiusSm, fontSize: 12.5 }}>{DEV_PASSWORD}</code>
          </div>
        </div>
      </div>
    </div>
  );
}
