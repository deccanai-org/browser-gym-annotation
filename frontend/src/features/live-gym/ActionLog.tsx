/**
 * The trajectory, building itself as the annotator works.
 *
 * This is what makes "every click is being recorded" *felt* rather than merely
 * true. Without it an annotator has no way to tell whether their work is being
 * captured until they finish and look — and by then a lost interaction is a lost
 * hour.
 *
 * Two states per row matter and are deliberately distinct:
 *   unverified  recorded, not yet proven to replay
 *   verified    proven, in a scratch world (see POST /certify)
 *   diverged    did NOT replay — evidence to fix, never silently dropped
 *   needs_value the value was redacted at record time, so it cannot be replayed
 *               until the annotator supplies it
 */
import { t, weight } from "../../ds";

export interface LoggedStep {
  stepId: string;
  index: number;
  actionType: string;
  description: string;
  replayState?: string;
  screenshotUrl?: string;
  tabId?: string;
  pending?: boolean;
}

const STATE_STYLE: Record<string, { dot: string; label: string }> = {
  verified: { dot: "#16a34a", label: "verified" },
  diverged: { dot: "#dc2626", label: "did not replay" },
  needs_value: { dot: "#d97706", label: "needs a value" },
  failed: { dot: "#dc2626", label: "failed" },
  unverified: { dot: "#94a3b8", label: "not yet proven" },
};

export interface ActionLogProps {
  steps: LoggedStep[];
  /** Live counters from the recorder. Non-zero `dropped` is not cosmetic — it
   *  means the trajectory is incomplete from that point on. */
  queued?: number;
  dropped?: number;
  onCertify?: () => void;
  certifying?: boolean;
}

export function ActionLog({ steps, queued = 0, dropped = 0, onCertify, certifying }: ActionLogProps) {
  const counts = steps.reduce<Record<string, number>>((acc, s) => {
    const k = s.replayState || "unverified";
    acc[k] = (acc[k] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <aside
      aria-label="Recorded actions"
      style={{
        display: "flex", flexDirection: "column", width: 320, minWidth: 260,
        borderLeft: `1px solid ${t.n7}`, background: t.n9, overflow: "hidden",
      }}
    >
      <header style={{ padding: "10px 12px", borderBottom: `1px solid ${t.n7}`, display: "flex",
                       alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <div>
          <div style={{ fontWeight: weight.semibold, fontSize: "0.82rem" }}>
            Trajectory · {steps.length} step{steps.length === 1 ? "" : "s"}
          </div>
          <div style={{ fontSize: "0.7rem", color: t.n3 }}>
            {counts.verified ? `${counts.verified} verified · ` : ""}
            {counts.diverged ? `${counts.diverged} diverged · ` : ""}
            recording as you work
          </div>
        </div>
        {onCertify && (
          <button
            onClick={onCertify}
            disabled={certifying || !steps.length}
            title="Replay these steps in a scratch world. Your own world is not touched."
            style={{
              padding: "5px 10px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`,
              background: "transparent", fontSize: "0.72rem", fontWeight: weight.semibold,
              cursor: certifying || !steps.length ? "not-allowed" : "pointer",
              opacity: certifying || !steps.length ? 0.5 : 1, whiteSpace: "nowrap",
            }}
          >
            {certifying ? "Checking…" : "Check"}
          </button>
        )}
      </header>

      {dropped > 0 && (
        // Loud on purpose: silently losing the middle of a task is the one
        // failure an annotator can neither see nor recover from.
        <div role="alert" style={{ padding: "8px 12px", background: "#7f1d1d", color: "#fff", fontSize: "0.72rem" }}>
          {dropped} interaction{dropped === 1 ? "" : "s"} were lost — the trajectory is
          incomplete from here.
        </div>
      )}

      <ol style={{ listStyle: "none", margin: 0, padding: 0, overflowY: "auto", flex: 1 }}>
        {steps.length === 0 && (
          <li style={{ padding: 16, color: t.n3, fontSize: "0.76rem" }}>
            Nothing recorded yet. Work through the task in the browser — each action
            appears here.
          </li>
        )}
        {steps.map((s) => {
          const st = STATE_STYLE[s.replayState || "unverified"] ?? STATE_STYLE.unverified;
          return (
            <li
              key={s.stepId}
              style={{
                display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 12px",
                borderBottom: `1px solid ${t.n8}`, opacity: s.pending ? 0.55 : 1,
              }}
            >
              <span style={{ color: t.n4, fontSize: "0.7rem", minWidth: 18, textAlign: "right",
                             fontVariantNumeric: "tabular-nums" }}>
                {s.index + 1}
              </span>
              <span aria-hidden style={{ width: 8, height: 8, borderRadius: 999, marginTop: 5,
                                         background: st.dot, flex: "0 0 auto" }} />
              <span style={{ flex: 1, minWidth: 0 }}>
                <span style={{ display: "block", fontSize: "0.78rem", overflowWrap: "anywhere" }}>
                  {s.description || s.actionType}
                </span>
                <span style={{ fontSize: "0.68rem", color: t.n3 }}>
                  {s.pending ? "sending…" : st.label}
                  {s.tabId ? ` · ${s.tabId}` : ""}
                </span>
              </span>
            </li>
          );
        })}
      </ol>

      {queued > 0 && (
        <footer style={{ padding: "6px 12px", borderTop: `1px solid ${t.n7}`, fontSize: "0.68rem", color: t.n3 }}>
          {queued} queued
        </footer>
      )}
    </aside>
  );
}

export default ActionLog;
