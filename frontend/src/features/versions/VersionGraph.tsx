import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Button, Icon, t, tint, weight, Pressable } from "../../ds";
import {
  buildLineage,
  createFork,
  ensureBaseline,
  fetchRuns,
  fetchVersionGraph,
  headOf,
  KIND_LABEL,
  runsLeft,
  selectHeadOrReload,
  setStatusOrReload,
  type ForkPoint,
  type RunsData,
  type VersionGraphData,
  type VersionNode,
  type VersionStatus,
} from "../../lib/versionsApi";

/**
 * The attempt's lineage: v1 canonical run → corrections → human edits.
 *
 * Two different things are true of a row and the design keeps them apart on
 * purpose: HEAD is the attempt's answer, VIEWING is just what the annotator is
 * reading. A finished agent run lands here as a candidate and stays one until
 * somebody selects it.
 */

const STATUS_TONE: Record<VersionStatus, { fg: string; label: string }> = {
  candidate: { fg: t.n2, label: "candidate" },
  approved: { fg: t.greenDark, label: "approved" },
  rejected: { fg: t.redDark, label: "rejected" },
  published: { fg: t.primary6, label: "published" },
};

/** The sentence an annotator needs the moment the cap stops them. The server
 *  says the same thing in its refusal (backend/app/agent_runs.py `EXHAUSTED`);
 *  a cap that only says no leaves them with no way to finish the attempt. */
export const MANUAL_FALLBACK =
  "Finish this attempt by hand: reject the step you disagree with, then commit your own actions on that branch — each one is replay-validated before it is kept.";

export interface RunBudget {
  cap: number;
  /** Runs the annotator can still start. */
  left: number;
  /** Started but not yet landed — already reserved against the cap. */
  reserved: number;
  /** Failures our side owned, which were given back. */
  refunded: number;
}

/**
 * What is left to spend, counted the way the server counts it.
 *
 * `runsLeft` only knows about runs that have LANDED, because that is all the
 * attempt's counter holds. A queued or running job has already reserved its run
 * (backend/app/agent_runs.py, `spent`), so a number that ignored those would
 * promise a run the very next click is refused — and being refused against a
 * number you were just shown is exactly how a cap loses an annotator's trust.
 */
export function runBudget(runs: RunsData | null | undefined): RunBudget | null {
  const landed = runsLeft(runs ?? null);
  if (!runs || runs.cap == null || landed == null) return null;
  const reserved = runs.runs.filter((r) => r.countsAgainstCap && (r.status === "queued" || r.status === "running")).length;
  const refunded = runs.runs.filter((r) => r.status === "error" && !r.countsAgainstCap).length;
  return { cap: runs.cap, left: Math.max(0, landed - reserved), reserved, refunded };
}

/**
 * The attempt's run budget, read from the server.
 *
 * Sourced from the graph's own attempt id rather than handed down as a prop: the
 * panel is mounted (TaskReview's `LineagePanel`) without a runs payload, and a
 * budget that appears only when some parent remembers to pass one is a budget
 * nobody ever reads. It has to be on screen BEFORE a run is spent.
 *
 * `changed` re-reads it when the lineage moves, because a landed run arrives as
 * a new candidate version and has just changed the count.
 */
export function useAgentRuns(attemptId: string | null, changed?: unknown): RunsData | null {
  const [runs, setRuns] = useState<RunsData | null>(null);
  useEffect(() => {
    if (!attemptId) {
      setRuns(null);
      return;
    }
    let live = true;
    // A failed read leaves the last known number standing. Blanking it would
    // read as "no cap" on a capped attempt, which is the one wrong answer here.
    void fetchRuns(attemptId).then((res) => {
      if (live && res.ok) setRuns(res.value);
    });
    return () => {
      live = false;
    };
  }, [attemptId, changed]);
  return runs;
}

/** The budget, spelled out. Every line here exists because the annotator has to
 *  be able to act on the number: what is left, what is already committed, what
 *  we gave back, and what to do when there is nothing left. */
function RunBudgetPanel({ budget }: { budget: RunBudget }) {
  const out = budget.left === 0;
  const line = { fontSize: "0.6875rem", lineHeight: 1.45, color: t.n2 } as const;
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        padding: "10px 16px",
        borderBottom: `1px solid ${t.n7}`,
        background: out ? tint(t.yellow, 14) : t.n85,
      }}
    >
      <span style={{ fontSize: "0.719rem", fontWeight: weight.bold, color: out ? t.yellowDark : t.n1 }}>
        {out
          ? `No agent runs left — all ${budget.cap} are spent`
          : `${budget.left} of ${budget.cap} agent ${budget.cap === 1 ? "run" : "runs"} left`}
      </span>
      {budget.reserved > 0 && (
        <span style={line}>
          {budget.reserved} {budget.reserved === 1 ? "run is" : "runs are"} still going and already counted, so this
          number cannot offer you a run that would then be refused.
        </span>
      )}
      {budget.refunded > 0 && (
        <span style={line}>
          {budget.refunded} {budget.refunded === 1 ? "run" : "runs"} failed on our side and{" "}
          {budget.refunded === 1 ? "was" : "were"} not counted.
        </span>
      )}
      <span style={{ ...line, color: out ? t.n1 : t.n3, fontWeight: out ? weight.medium : weight.regular }}>
        {out
          ? MANUAL_FALLBACK
          : budget.left === 1
            ? "This is the last one. After it, the attempt is finished by hand."
            : "Counted per attempt — branching does not start a new budget."}
      </span>
    </div>
  );
}

function StatusChip({ status }: { status: VersionStatus }) {
  const tone = STATUS_TONE[status] ?? STATUS_TONE.candidate;
  return (
    <span
      style={{
        fontSize: "0.594rem",
        fontWeight: weight.bold,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        color: tone.fg,
        background: tint(tone.fg, 12),
        padding: "2px 6px",
        borderRadius: t.radiusSm,
        whiteSpace: "nowrap",
      }}
    >
      {tone.label}
    </span>
  );
}

function Pill({ children, onClick, disabled, tone = t.primary6 }: { children: ReactNode; onClick: () => void; disabled?: boolean; tone?: string }) {
  return (
    <Pressable
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "4px 10px",
        borderRadius: t.radiusLg,
        border: `1px solid ${t.n6}`,
        background: t.n9,
        color: tone,
        fontSize: "0.719rem",
        fontWeight: weight.semibold,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </Pressable>
  );
}

/** The banner a lost compare-and-swap produces. It is not an error toast: the
 *  write did not happen, the lineage below has already been re-read, and the
 *  annotator has to look again before deciding. */
function MovedOnNotice({ notice, onDismiss }: { notice: string; onDismiss: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "10px 14px",
        background: tint(t.yellow, 14),
        borderBottom: `1px solid ${t.n7}`,
      }}
    >
      <Icon name="alert" size={14} color={t.yellowDark} style={{ flexShrink: 0, marginTop: 1 }} />
      <span style={{ flex: 1, fontSize: "0.719rem", lineHeight: 1.45, color: t.n1, fontWeight: weight.medium }}>{notice}</span>
      <Pressable onClick={onDismiss} label="Dismiss this notice" style={{ flexShrink: 0, marginTop: 1 }}>
        <Icon name="close" size={13} color={t.n3} />
      </Pressable>
    </div>
  );
}

export function VersionGraph({
  graph,
  runs,
  viewingId,
  notice,
  busyVersionId,
  onView,
  onSelectHead,
  onSetStatus,
  onDismissNotice,
  onCreateBaseline,
}: {
  graph: VersionGraphData | null;
  runs?: RunsData | null;
  viewingId: string | null;
  notice: string | null;
  busyVersionId?: string | null;
  onView: (versionId: string) => void;
  onSelectHead: (v: VersionNode) => void;
  onSetStatus: (v: VersionNode, status: VersionStatus) => void;
  onDismissNotice: () => void;
  onCreateBaseline?: () => void;
}) {
  const rows = graph ? buildLineage(graph.versions) : [];
  const head = headOf(graph);
  // Read here when the caller does not hold it, so the number is on screen at
  // the mount the app actually renders rather than only where a test passes it.
  const sourced = useAgentRuns(runs ? null : graph?.attemptId ?? null, graph?.versions.length);
  const budget = runBudget(runs ?? sourced);
  // The payload carries the fork POINT but not the fork MODE, so a row can name
  // the version it branched from and must not claim which step it rejected.
  const numberOf = new Map((graph?.versions ?? []).map((v) => [v.id, v.versionNo]));

  return (
    <div style={{ background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, overflow: "hidden" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "12px 16px", borderBottom: `1px solid ${t.n7}` }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: "0.8125rem", fontWeight: weight.bold, color: t.n1 }}>
          <Icon name="branch" size={14} color={t.n3} /> Version lineage
        </span>
        <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", color: t.n3 }}>
          {head ? `head v${head.versionNo}` : "no head yet"}
          {budget ? ` · ${budget.left}/${budget.cap} runs` : ""}
        </span>
      </div>

      {budget && <RunBudgetPanel budget={budget} />}

      {notice && <MovedOnNotice notice={notice} onDismiss={onDismissNotice} />}

      {rows.length === 0 ? (
        <div style={{ padding: "18px 16px", display: "flex", flexDirection: "column", gap: 10 }}>
          <span style={{ fontSize: "0.75rem", color: t.n2, lineHeight: 1.5 }}>
            No lineage yet. v1 is the canonical agent run this attempt annotates; every correction hangs off it.
          </span>
          {onCreateBaseline && (
            <Button variant="secondary" onClick={onCreateBaseline}>
              Create baseline v1
            </Button>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {rows.map(({ version: v, depth }) => {
            const viewing = v.id === viewingId;
            const busy = busyVersionId === v.id;
            return (
              <div
                key={v.id}
                style={{
                  display: "flex",
                  gap: 8,
                  padding: "11px 16px 11px 12px",
                  borderTop: `1px solid ${t.n8}`,
                  // HEAD is the loud one. Viewing only gets a tint, so "I am
                  // reading v3" can never be mistaken for "v3 is the answer".
                  background: v.isHead ? tint(t.primary6, 7) : viewing ? t.surfaceTint : "transparent",
                  borderLeft: `3px solid ${v.isHead ? t.primary6 : "transparent"}`,
                }}
              >
                {depth > 0 && (
                  <span aria-hidden style={{ width: depth * 12, flexShrink: 0, display: "flex", justifyContent: "flex-end", paddingTop: 8 }}>
                    <span style={{ width: 9, height: 9, borderLeft: `1px solid ${t.n6}`, borderBottom: `1px solid ${t.n6}`, borderBottomLeftRadius: t.radiusSm }} />
                  </span>
                )}

                <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 5 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
                    <Pressable
                      onClick={() => onView(v.id)}
                      label={`Open version ${v.versionNo}`}
                      style={{ fontFamily: t.fontMono, fontSize: "0.8125rem", fontWeight: weight.bold, color: viewing ? t.primary6 : t.n0 }}
                    >
                      v{v.versionNo}
                    </Pressable>
                    <span style={{ fontSize: "0.719rem", color: t.n2 }}>{KIND_LABEL[v.kind] ?? v.kind}</span>
                    {v.isHead && (
                      <span
                        style={{
                          fontSize: "0.594rem",
                          fontWeight: weight.black,
                          letterSpacing: "0.06em",
                          color: t.n9,
                          background: t.primary6,
                          padding: "2px 7px",
                          borderRadius: t.radiusSm,
                        }}
                      >
                        HEAD
                      </span>
                    )}
                    <StatusChip status={v.status} />
                    {viewing && !v.isHead && (
                      <span style={{ fontSize: "0.594rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: t.n3 }}>viewing</span>
                    )}
                  </div>

                  <span style={{ fontSize: "0.6875rem", color: t.n3, fontFamily: t.fontMono }}>
                    {v.stepCount} {v.stepCount === 1 ? "step" : "steps"}
                    {v.parentId && numberOf.has(v.parentId) ? ` · from v${numberOf.get(v.parentId)}` : ""}
                    {v.producer ? ` · ${v.producer}` : ""}
                  </span>

                  <span style={{ fontSize: "0.6875rem", lineHeight: 1.45, color: v.isHead ? t.primary6 : t.n2 }}>
                    {v.isHead
                      ? "The attempt's current answer."
                      : "Candidate — it is not the answer until you select it."}
                  </span>

                  {/* QC status is offered on the head too: finalize refuses to
                      ship a version that is not approved, and the head is the
                      one an annotator will try to ship. */}
                  <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
                    {!v.isHead && (
                      <Pill onClick={() => onSelectHead(v)} disabled={busy}>
                        {busy ? "Selecting…" : `Make v${v.versionNo} the head`}
                      </Pill>
                    )}
                    {v.status !== "approved" && (
                      <Pill tone={t.greenDark} onClick={() => onSetStatus(v, "approved")} disabled={busy}>
                        Approve
                      </Pill>
                    )}
                    {v.status !== "rejected" && (
                      <Pill tone={t.redDark} onClick={() => onSetStatus(v, "rejected")} disabled={busy}>
                        Reject
                      </Pill>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ padding: "9px 16px", borderTop: `1px solid ${t.n8}`, background: t.n85 }}>
        <span style={{ fontSize: "0.6875rem", color: t.n3, lineHeight: 1.45 }}>
          Opening a version only reads it. An agent run finishes as a candidate and never takes the head on its own.
        </span>
      </div>
    </div>
  );
}

/**
 * Owns the lineage for one attempt: the graph, which version is being read, and
 * every compare-and-swap against it.
 *
 * `graph.revision` is the ATTEMPT's revision and guards head selection;
 * `version.revision` is per-version and guards a QC status change. They are
 * separate counters — passing one where the other belongs would make every
 * second call 409 for no reason.
 */
export function useVersionGraph(sessionId: string | null) {
  const [graph, setGraph] = useState<VersionGraphData | null>(null);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // A refresh must not yank the annotator to another version mid-read: keep
  // what they were looking at, and only fall back to head when it is gone.
  const adopt = useCallback((g: VersionGraphData) => {
    setGraph(g);
    setViewingId((cur) => (cur && g.versions.some((v) => v.id === cur) ? cur : g.headVersionId ?? g.versions[g.versions.length - 1]?.id ?? null));
  }, []);

  const refresh = useCallback(async () => {
    if (!sessionId) return null;
    setLoading(true);
    const res = await fetchVersionGraph(sessionId);
    setLoading(false);
    if (!res.ok) {
      // Offline is already a first-class state everywhere else in this app —
      // degrade quietly rather than shouting at an annotator with no backend.
      if (res.kind !== "offline") setNotice(res.message);
      return null;
    }
    adopt(res.value);
    return res.value;
  }, [sessionId, adopt]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selectHead = useCallback(
    async (v: VersionNode) => {
      if (!sessionId || !graph) return;
      setBusyVersionId(v.id);
      const out = await selectHeadOrReload(sessionId, v.id, graph.revision);
      if (out.reloaded) adopt(out.reloaded);
      else if (out.ok) await refresh();
      setNotice(out.notice);
      setBusyVersionId(null);
    },
    [sessionId, graph, adopt, refresh],
  );

  const setStatus = useCallback(
    async (v: VersionNode, status: VersionStatus) => {
      if (!sessionId) return;
      setBusyVersionId(v.id);
      const out = await setStatusOrReload(sessionId, v.id, status, v.revision);
      if (out.reloaded) adopt(out.reloaded);
      else if (out.ok) await refresh();
      setNotice(out.notice);
      setBusyVersionId(null);
    },
    [sessionId, adopt, refresh],
  );

  /** Branch. The child is opened for reading — deliberately NOT made head. */
  const fork = useCallback(
    async (point: ForkPoint) => {
      if (!sessionId) return null;
      const res = await createFork(sessionId, point);
      if (!res.ok) {
        setNotice(res.message);
        return null;
      }
      await refresh();
      setViewingId(res.value.id);
      return res.value;
    },
    [sessionId, refresh],
  );

  const baseline = useCallback(async () => {
    if (!sessionId) return;
    const res = await ensureBaseline(sessionId);
    if (!res.ok) setNotice(res.message);
    await refresh();
  }, [sessionId, refresh]);

  return {
    graph,
    viewingId,
    notice,
    loading,
    busyVersionId,
    head: headOf(graph),
    view: setViewingId,
    refresh,
    selectHead,
    setStatus,
    fork,
    baseline,
    dismissNotice: () => setNotice(null),
  };
}
