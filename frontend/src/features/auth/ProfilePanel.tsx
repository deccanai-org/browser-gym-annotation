import { avatarColor, t, weight } from "../../ds";
import { useAuth } from "./AuthContext";

const C = { ink: t.n0, muted: t.n2, faint: t.n3, border: t.n7, card: t.n9, chip: t.n8, danger: t.redDark };

function initials(name: string): string {
  const p = name.trim().split(/\s+/);
  return ((p[0]?.[0] ?? "") + (p[1]?.[0] ?? "")).toUpperCase() || name.slice(0, 2).toUpperCase();
}

export function ProfilePanel({ onClose }: { onClose: () => void }) {
  const { annotator, signOut } = useAuth();
  if (!annotator) return null;
  const a = annotator;
  const s = a.stats ?? { sessions: 0, submitted: 0, golden: 0, breaker: 0, flagged: 0 };
  const stat = (label: string, value: number, hue?: string) => (
    <div style={{ background: C.chip, borderRadius: t.radiusLg, padding: "12px 14px", minWidth: 84 }}>
      <div style={{ fontFamily: t.fontMono, fontSize: 22, fontWeight: weight.bold, color: hue ?? C.ink, fontVariantNumeric: "tabular-nums" }}>{value}</div>
      <div style={{ fontSize: 11.5, color: C.muted, marginTop: 2 }}>{label}</div>
    </div>
  );

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "var(--overlay-backdrop)", display: "flex", alignItems: "flex-start", justifyContent: "center", zIndex: 1000, padding: "70px 20px", fontFamily: t.fontPrimary }}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: C.card, borderRadius: t.radius2xl, width: "100%", maxWidth: 420, boxShadow: t.shadowCard, overflow: "hidden" }}>
        <div style={{ padding: "22px 22px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <span style={{ width: 52, height: 52, borderRadius: t.radiusFull, background: avatarColor(a.avatarHue), color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: weight.bold, fontSize: 19, flexShrink: 0 }}>{initials(a.displayName)}</span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontFamily: t.fontDisplay, fontSize: 17, fontWeight: weight.semibold, color: C.ink, letterSpacing: t.trackingHeading }}>{a.displayName}</div>
            <div style={{ fontSize: 13, color: C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{a.email}</div>
            <span style={{ display: "inline-block", marginTop: 5, fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: t.trackingEyebrow, color: t.primary7, background: t.primary0, borderRadius: t.radiusPill, padding: "2px 9px" }}>{a.role}</span>
          </div>
        </div>
        <div style={{ padding: "0 22px 8px", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {stat("Sessions", s.sessions)}
          {stat("Submitted", s.submitted)}
          {stat("Golden", s.golden, t.greenDark)}
          {stat("Breakers", s.breaker, t.yellowDark)}
          {stat("Flagged", s.flagged, C.danger)}
        </div>
        <div style={{ padding: "14px 22px", fontSize: 12, color: C.faint }}>
          {a.lastLoginAt ? `Last login ${new Date(a.lastLoginAt).toLocaleString()}` : "First session"}
        </div>
        <div style={{ borderTop: `1px solid ${C.border}`, padding: "14px 22px", display: "flex", justifyContent: "space-between", gap: 10 }}>
          <button onClick={onClose} style={{ border: `1px solid ${C.border}`, background: t.n9, color: C.ink, borderRadius: t.radiusLg, padding: "9px 16px", fontSize: 13.5, fontWeight: weight.semibold, cursor: "pointer" }}>Close</button>
          <button onClick={() => void signOut()} style={{ border: "none", background: t.redLite, color: C.danger, borderRadius: t.radiusLg, padding: "9px 16px", fontSize: 13.5, fontWeight: weight.bold, cursor: "pointer" }}>Log out</button>
        </div>
      </div>
    </div>
  );
}
