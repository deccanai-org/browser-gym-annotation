import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ACTION_COLOR, Icon, Pressable, t, tint, weight, type ActionType } from "../../ds";
import {
  applyVerdict,
  fetchVersionSteps,
  FORK_COPY,
  setStepVerdict,
  verdictTally,
  type StepVerdict,
  type VersionNode,
  type VersionStep,
} from "../../lib/versionsApi";

/**
 * The flattened steps of ONE version: the prefix it inherits from its parent
 * plus its own suffix, numbered for display only.
 *
 * Inherited rows are the same step rows as the parent's — not copies — which is
 * why a verdict recorded here shows up on that step in every other branch that
 * contains it. The badge says so, because an annotator who reads them as copies
 * will re-review the same prefix once per branch.
 */

const VERDICT_TONE: Record<StepVerdict, string> = {
  verified: t.green,
  rejected: t.red,
  pending: t.n5,
};

function VerdictDot({ verdict }: { verdict: StepVerdict }) {
  if (verdict === "verified") {
    return (
      <span style={{ width: 16, height: 16, borderRadius: t.radiusFull, background: t.green, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <Icon name="check" size={10} stroke={2.8} color={t.n9} />
      </span>
    );
  }
  if (verdict === "rejected") {
    return (
      <span style={{ width: 16, height: 16, borderRadius: t.radiusFull, background: t.red, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <Icon name="close" size={9} stroke={2.8} color={t.n9} />
      </span>
    );
  }
  return (
    <span style={{ width: 16, height: 16, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
      <span style={{ width: 11, height: 11, borderRadius: t.radiusFull, border: `2px solid ${VERDICT_TONE.pending}` }} />
    </span>
  );
}

function Action({ children, hint, tone, onClick, disabled }: { children: ReactNode; hint: string; tone: string; onClick: (e: React.MouseEvent) => void; disabled?: boolean }) {
  return (
    <div style={{ flex: 1, minWidth: 190, display: "flex", flexDirection: "column", gap: 4 }}>
      <Pressable
        onClick={onClick}
        disabled={disabled}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 6,
          padding: "6px 12px",
          borderRadius: t.radiusLg,
          border: `1px solid ${tone}`,
          background: tint(tone, 8),
          color: tone,
          fontSize: "0.75rem",
          fontWeight: weight.bold,
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
        }}
      >
        {children}
      </Pressable>
      <span style={{ fontSize: "0.6875rem", lineHeight: 1.4, color: t.n3 }}>{hint}</span>
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div style={{ display: "flex", gap: 8 }}>
      <span style={{ width: 62, flexShrink: 0, fontSize: "0.6875rem", fontWeight: weight.bold, color: t.n3, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</span>
      <span style={{ flex: 1, minWidth: 0, fontSize: "0.719rem", lineHeight: 1.5, color: t.n1 }}>{value}</span>
    </div>
  );
}

export function VersionSteps({
  version,
  steps,
  loading = false,
  versionNos = {},
  selectedStepId,
  busyStepId,
  onSelectStep,
  onVerdict,
  onRejectStep,
  onContinueAfter,
}: {
  version: VersionNode | null;
  steps: VersionStep[];
  loading?: boolean;
  /** version id → version number, so an inherited row can name its origin. */
  versionNos?: Record<string, number>;
  selectedStepId: string | null;
  busyStepId?: string | null;
  onSelectStep: (stepId: string) => void;
  onVerdict: (step: VersionStep, verdict: StepVerdict) => void;
  /** Fork BEFORE this step — it will not appear in the child. */
  onRejectStep: (step: VersionStep) => void;
  /** Fork AFTER this step — it is kept. */
  onContinueAfter: (step: VersionStep) => void;
}) {
  const tally = verdictTally(steps);
  const originOf = (step: VersionStep) => (step.versionId ? versionNos[step.versionId] : undefined);

  // A fork is not idempotent: every call mints a new candidate version. The
  // handlers live in the parent, so the guard has to be HERE — otherwise a
  // double-click (or an impatient second click while the request is in flight)
  // silently creates two branches off the same step, and the annotator has to
  // work out which of two identical-looking candidates to keep.
  const [forking, setForking] = useState(false);
  const forkingRef = useRef(false);
  const fork = (run: (step: VersionStep) => void | Promise<void>) => (step: VersionStep, e?: React.MouseEvent) => {
    // `detail` counts the clicks in the current sequence, so the second click of
    // a double-click is exactly detail === 2. That is the precise signal: elapsed
    // time cannot tell a double-click from a deliberate re-fork, and the promise
    // latch below only guards callers that return one — a synchronous handler
    // settles between the two clicks and forks twice (measured).
    if (e && e.detail > 1) return;
    if (forkingRef.current) return;
    forkingRef.current = true;
    setForking(true);
    void Promise.resolve(run(step)).finally(() => {
      forkingRef.current = false;
      setForking(false);
    });
  };
  const rejectStep = (s: VersionStep, e: React.MouseEvent) => fork(onRejectStep)(s, e);
  const continueAfter = (s: VersionStep, e: React.MouseEvent) => fork(onContinueAfter)(s, e);

  return (
    <div style={{ background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, display: "flex", flexDirection: "column", overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "12px 18px", borderBottom: `1px solid ${t.n7}` }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: "0.8125rem", fontWeight: weight.bold, color: t.n1 }}>
            Steps in {version ? `v${version.versionNo}` : "this version"}
          </span>
          {version?.isHead && (
            <span style={{ fontSize: "0.594rem", fontWeight: weight.black, letterSpacing: "0.06em", color: t.n9, background: t.primary6, padding: "2px 7px", borderRadius: t.radiusSm }}>HEAD</span>
          )}
        </span>
        <span style={{ fontFamily: t.fontMono, fontSize: "0.719rem", color: t.n3 }}>
          <span style={{ color: t.greenDark, fontWeight: weight.bold }}>{tally.verified} verified</span> · <span style={{ color: t.redDark, fontWeight: weight.bold }}>{tally.rejected} wrong</span> · {tally.pending} pending
        </span>
      </div>

      <div style={{ padding: "8px 18px", borderBottom: `1px solid ${t.n8}`, background: t.n85 }}>
        <span style={{ fontSize: "0.6875rem", lineHeight: 1.45, color: t.n3 }}>
          A verdict is recorded against the step itself, so it follows that step into every branch that keeps it.
        </span>
      </div>

      <div style={{ maxHeight: 420, overflowY: "auto", overscrollBehavior: "contain" }}>
        {loading && steps.length === 0 && (
          <div style={{ padding: "18px", fontSize: "0.75rem", color: t.n3 }}>Loading steps…</div>
        )}
        {!loading && steps.length === 0 && (
          <div style={{ padding: "18px", fontSize: "0.75rem", color: t.n3 }}>This version has no steps yet.</div>
        )}
        {steps.map((s) => {
          const selected = s.stepId === selectedStepId;
          const busy = busyStepId === s.stepId;
          const origin = originOf(s);
          const hue = ACTION_COLOR[s.type as ActionType] ?? t.n3;
          return (
            <div key={s.stepId} style={{ borderTop: `1px solid ${t.n8}` }}>
              <Pressable
                onClick={() => onSelectStep(s.stepId)}
                label={`Step ${s.displayIdx + 1}: ${s.description}`}
                style={{
                  width: "100%",
                  display: "flex",
                  alignItems: "center",
                  gap: 11,
                  padding: "11px 18px",
                  cursor: "pointer",
                  background: selected ? t.surfaceTint : "transparent",
                  borderLeft: `3px solid ${selected ? t.primary6 : "transparent"}`,
                  transition: t.transitionUi,
                }}
              >
                <span style={{ fontFamily: t.fontMono, fontSize: "0.75rem", color: selected ? t.primary6 : t.n3, width: 24, flexShrink: 0 }}>
                  {String(s.displayIdx + 1).padStart(2, "0")}
                </span>
                <VerdictDot verdict={s.verdict} />
                <span style={{ width: 8, height: 8, borderRadius: t.radiusFull, background: hue, flexShrink: 0 }} />
                <span style={{ fontSize: "0.65rem", fontWeight: weight.bold, letterSpacing: "0.04em", textTransform: "uppercase", color: hue, width: 62, flexShrink: 0 }}>{s.type}</span>
                <span style={{ flex: 1, minWidth: 0, fontSize: "0.84rem", color: t.n0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.description}</span>
                {s.actor === "human" && (
                  <span style={{ flexShrink: 0, fontSize: "0.594rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: t.purple, background: tint(t.purple, 12), padding: "2px 6px", borderRadius: t.radiusSm }}>human</span>
                )}
                {s.inherited && (
                  <span
                    title="Shared with the parent version — the same step row. Its verdict is the same everywhere it appears."
                    style={{ flexShrink: 0, fontSize: "0.594rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: t.n2, background: t.n8, border: `1px solid ${t.n7}`, padding: "2px 6px", borderRadius: t.radiusSm }}
                  >
                    {origin ? `inherited · v${origin}` : "inherited"}
                  </span>
                )}
              </Pressable>

              {selected && (
                <div style={{ padding: "2px 18px 16px 36px", display: "flex", flexDirection: "column", gap: 12, background: t.surfaceTint }}>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <Detail label="Why" value={s.reasoning} />
                    <Detail label="Intent" value={s.humanIntent} />
                    <Detail label="Guidance" value={s.guidance} />
                    <Detail label="URL" value={s.url} />
                  </div>

                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span style={{ fontSize: "0.6875rem", fontWeight: weight.bold, color: t.n3, textTransform: "uppercase", letterSpacing: "0.04em", width: 62 }}>Verdict</span>
                    {(["verified", "rejected", "pending"] as const).map((v) => (
                      <Pressable
                        key={v}
                        onClick={() => onVerdict(s, v)}
                        disabled={busy}
                        pressed={s.verdict === v}
                        label={`Mark step ${s.displayIdx + 1} ${v === "verified" ? "verified" : v === "rejected" ? "wrong" : "not reviewed"}`}
                        style={{
                          padding: "4px 11px",
                          borderRadius: t.radiusLg,
                          border: `1px solid ${s.verdict === v ? VERDICT_TONE[v] : t.n6}`,
                          background: s.verdict === v ? tint(VERDICT_TONE[v], 14) : t.n9,
                          color: s.verdict === v ? VERDICT_TONE[v] : t.n2,
                          fontSize: "0.719rem",
                          fontWeight: weight.semibold,
                          cursor: busy ? "not-allowed" : "pointer",
                          opacity: busy ? 0.5 : 1,
                        }}
                      >
                        {v === "verified" ? "Verified" : v === "rejected" ? "Wrong" : "Not reviewed"}
                      </Pressable>
                    ))}
                  </div>

                  <div style={{ display: "flex", gap: 14, flexWrap: "wrap", paddingTop: 2, borderTop: `1px solid ${t.n7}`, marginTop: 2 }}>
                    {/* Two separate commands with two separate sentences. A single
                        button with a before/after toggle is how a rejected action
                        ends up kept in the golden trajectory. */}
                    <Action tone={t.redDark} hint={FORK_COPY.before.hint} onClick={(e) => rejectStep(s, e)} disabled={busy || forking}>
                      <Icon name="branch" size={13} color={t.redDark} /> {FORK_COPY.before.action}
                    </Action>
                    <Action tone={t.primary6} hint={FORK_COPY.after.hint} onClick={(e) => continueAfter(s, e)} disabled={busy || forking}>
                      <Icon name="chevronRight" size={13} color={t.primary6} /> {FORK_COPY.after.action}
                    </Action>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * Loads and mutates the flattened steps of the version currently being read.
 *
 * A verdict is applied locally only AFTER the write lands: showing it first
 * would leave the annotator believing a step is reviewed when the server never
 * heard about it, and the whole point of per-step verdicts is that they are
 * durable across forks.
 */
export function useVersionSteps(sessionId: string | null, versionId: string | null) {
  const [steps, setSteps] = useState<VersionStep[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyStepId, setBusyStepId] = useState<string | null>(null);

  // Which fetch owns the view. Two versions' step lists look identical once
  // rendered, so a slow response for the version the annotator just left would
  // paint over the one they are reading — and they would then verdict and fork
  // against rows belonging to a different branch.
  const genRef = useRef(0);

  const reload = useCallback(async () => {
    const gen = ++genRef.current;
    if (!sessionId || !versionId) {
      setSteps([]);
      return;
    }
    setLoading(true);
    // Clear FIRST. Holding the previous version's rows while the new ones load
    // shows the wrong steps under the right header, which is worse than an empty
    // list because it looks correct.
    setSteps([]);
    const res = await fetchVersionSteps(sessionId, versionId);
    if (gen !== genRef.current) return; // a newer request already owns the view
    setLoading(false);
    if (!res.ok) {
      setError(res.message);
      return;
    }
    setError(null);
    setSteps(res.value.steps);
  }, [sessionId, versionId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const verdict = useCallback(
    async (step: VersionStep, next: StepVerdict) => {
      if (!sessionId) return;
      setBusyStepId(step.stepId);
      const res = await setStepVerdict(sessionId, step.stepId, next);
      setBusyStepId(null);
      if (!res.ok) {
        setError(res.message);
        return;
      }
      setError(null);
      setSteps((cur) => applyVerdict(cur, step.stepId, res.value.verdict));
    },
    [sessionId],
  );

  return { steps, loading, error, busyStepId, reload, verdict };
}
