import { Button, Icon, t, weight } from "../../../ds";

export function BenchmarkDock({
  reward,
  benchmarkRun,
  failing,
  total,
  canSubmit,
  submitted,
  submittedKind,
  submitError,
  onRun,
  onSubmit,
  submitNote,
}: {
  reward: number | null;
  benchmarkRun: boolean;
  failing: number;
  total: number;
  canSubmit: boolean;
  submitted: boolean;
  submittedKind?: string | null;
  submitError?: string | null;
  onRun: () => void;
  /** The LEGACY submit. Undefined on an attempt that has a version graph: that
   *  path builds its golden from the recorded run plus a branch and cannot
   *  express a rejected step, so the button is absent rather than disabled —
   *  a disabled control an annotator cannot explain is still a question. */
  onSubmit?: () => void;
  /** What stands in for the button when there is no legacy submit. Says where
   *  shipping happens instead, so the empty corner is never a dead end. */
  submitNote?: string;
}) {
  const numeralColor = reward == null ? t.n4 : reward === 1 ? t.greenDark : t.redDark;
  const numeral = reward == null ? "—" : String(reward);

  let sub: string;
  if (!benchmarkRun) sub = "Run the benchmark to score every verifier on the final state.";
  else if (reward === 1)
    // Strict gate: for a built suite reward 1 means every verifier passed.
    // A gym review carries the gym's own authoritative success verdict, which
    // can be 1 even if some derived milestone checks did not fire — flag that.
    sub = failing === 0
      ? `All ${total} verifiers passed — reward 1, ready to submit.`
      : `Reward 1 is the run's authoritative verdict, but ${failing} of ${total} verifier check${failing > 1 ? "s" : ""} did not pass — review before submitting.`;
  else sub = `${failing} of ${total} verifiers scored 0. Override to submit, or edit a verifier / correct the trace and re-run.`;

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, padding: "16px 22px", borderTop: `1px solid ${t.n7}`, background: t.n85 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <span style={{ fontFamily: t.fontMono, fontSize: "2.375rem", fontWeight: weight.bold, lineHeight: 1, color: numeralColor, minWidth: 38, textAlign: "center" }}>{numeral}</span>
        <div>
          <div style={{ fontSize: "0.6875rem", fontWeight: weight.bold, letterSpacing: "0.07em", color: t.n2, textTransform: "uppercase" }}>Benchmark reward</div>
          <div style={{ marginTop: 2, fontSize: "0.78rem", lineHeight: 1.45, color: t.n2, maxWidth: 560 }}>{sub}</div>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        {submitted ? (
          // Reward/kind here are the SERVER's authoritative values (via serverSubmission),
          // so the badge can't claim a golden the DB didn't write. 'flagged' = a safety
          // check was overridden — not a clean golden.
          (() => {
            const good = submittedKind === "golden";
            const bg = good ? t.greenLite : submittedKind === "flagged" ? t.redLite : t.n7;
            const fg = good ? t.greenDark : submittedKind === "flagged" ? t.redDark : t.n1;
            return (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8, padding: "10px 16px", borderRadius: 8, background: bg, color: fg, fontSize: "0.84rem", fontWeight: weight.bold }}>
                <Icon name="check" size={15} stroke={2.4} color={fg} /> Submitted to dataset · reward {reward} · {submittedKind ?? (reward === 1 ? "golden" : "breaker")}
              </span>
            );
          })()
        ) : (
          <>
            {submitError && (
              <span style={{ fontSize: "0.78rem", fontWeight: weight.semibold, color: t.redDark, maxWidth: 280 }}>{submitError}</span>
            )}
            <Button variant={benchmarkRun ? "secondary" : "primary"} onClick={onRun} style={{ minHeight: 44 }}>
              {benchmarkRun ? "Re-run benchmark" : "Run benchmark"}
            </Button>
            {onSubmit ? (
              <Button variant="primary" disabled={!canSubmit} onClick={onSubmit} style={{ minHeight: 44 }}>
                Approve &amp; submit to dataset
              </Button>
            ) : submitNote ? (
              <span style={{ maxWidth: 320, fontSize: "0.78rem", lineHeight: 1.45, fontWeight: weight.semibold, color: t.n2 }}>{submitNote}</span>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
