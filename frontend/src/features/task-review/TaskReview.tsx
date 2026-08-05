import { useEffect, useReducer, useRef, useState, type ReactNode } from "react";
import { ACTION_COLOR, Button, Icon, t, tint, weight } from "../../ds";
import {
  adjudicate,
  autogenVerifiers,
  downloadSampleBundle,
  driveForwardGym,
  fetchGymStatus,
  fetchQaSubmissions,
  fetchQaTasks,
  fetchGymTasks,
  fetchReview,
  fetchTasks,
  getManualReview,
  openSession,
  patchSession,
  rerunGymBranch,
  resumeGymReview,
  runGymReview,
  runVerifiers,
  saveSuite,
  submitSession,
  fetchSessionHistory,
} from "../../lib/api";
import { continuingAfter, FORK_COPY, headOf, rejecting, type VersionNode } from "../../lib/versionsApi";
import type { AutogenResult, HistoryRound, QaSubmission, QaTaskRow } from "../../lib/api";
import type { ReviewData, TaskListItem, Verifier } from "../../lib/types";
import {
  canSubmit,
  makeInitialState,
  offlineResults,
  reducer,
  reward,
  runSummary,
  sessionStatus,
  verifierPayloads,
  visibleSteps,
} from "../../lib/reviewMachine";
import { useAuth } from "../auth/AuthContext";
import { ProfilePanel } from "../auth/ProfilePanel";
import type { Annotator } from "../auth/authApi";
import { LiveBrowserPane } from "../live-gym/LiveBrowserPane";
import { ActionLog } from "../live-gym/ActionLog";
import type { LoggedStep } from "../live-gym/ActionLog";
import { certifyTrajectory, fetchHeadVersionId, fetchLiveSteps } from "../../lib/actionLog";
import { attachLiveBrowser, closeLiveBrowser, currentLiveBrowser, resetLiveWorld, type LiveSession, type RestoreProgress } from "../live-gym/liveSessionApi";
import { useVersionGraph, VersionGraph } from "../versions/VersionGraph";
import { useVersionSteps, VersionSteps } from "../versions/VersionSteps";
import { Header } from "./components/Header";
import { RightPanel } from "./components/RightPanel";
import { VerifierSuite } from "./components/VerifierSuite";

const TASK_ID = "GYM-2041";

/** Close a modal on Escape + move focus into it on open (a11y). */
function useModalA11y(onClose: () => void, ref: { current: HTMLDivElement | null }) {
  useEffect(() => {
    ref.current?.focus();
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose, ref]);
}
const DIALOG = { role: "dialog", "aria-modal": true, tabIndex: -1 } as const;

function SectionHeader({ n, title, subtitle, done, right }: { n: number; title: string; subtitle: string; done?: boolean; right?: ReactNode }) {
  const active = n === 1 || done;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "2px 4px 12px" }}>
      <span style={{ width: 22, height: 22, borderRadius: t.radiusFull, background: n === 1 ? t.primary6 : done ? t.green : t.n4, color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", fontFamily: t.fontMono, fontSize: "0.75rem", fontWeight: weight.bold }}>{n}</span>
      <span style={{ fontSize: "0.875rem", fontWeight: weight.bold, color: active ? t.n0 : t.n2 }}>{title}</span>
      <span style={{ fontSize: "0.78rem", color: t.n3 }}>{subtitle}</span>
      {right && <span style={{ marginLeft: "auto" }}>{right}</span>}
    </div>
  );
}

/**
 * The live browser is the only surface.
 *
 * There used to be a Replay/Live toggle, because the trajectory being annotated
 * was an agent's and the live browser was an optional side trip. The annotator
 * now performs the task themselves, so there is no recorded run to replay and
 * nothing to toggle between: the gym IS the workspace, and it must be what they
 * land on rather than something they have to go and find.
 */

/**
 * Where the live world came from, and the way back to a clean one.
 *
 * A workspace outlives the pane attached to it, so reopening now KEEPS whatever
 * the annotator built rather than resetting it. That is the behaviour they want
 * and also an invisible one: without this badge the only way to find out whether
 * an hour of cart-building survived is to go and look for it.
 *
 * The reset is here rather than hidden because closing the pane used to be how
 * you started over. Taking that away without putting something in its place
 * would strand anyone who has driven their world into a corner.
 */
export function WorldBadge({ world, isolated, restore, onReset, resetting }: {
  world?: "preserved" | "seeded" | "shared" | "cua-hub";
  isolated?: boolean;
  restore?: RestoreProgress | null;
  onReset: () => void;
  resetting: boolean;
}) {
  if (!world || world === "shared") return null;
  // A bridged realistic-gym attempt is its own case: a private cloned world in
  // the five real storefronts, driven by the live engine. Falling through to the
  // "fresh seed" branch would mislabel it AND offer a Reset that means something
  // different here.
  if (world === "cua-hub") {
    return (
      <span style={{
        display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px",
        borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9,
        fontSize: "0.75rem", fontWeight: weight.semibold, whiteSpace: "nowrap",
      }}>
        ● Realistic gym · live engine
      </span>
    );
  }
  const preserved = world === "preserved";
  const pill = {
    display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px",
    borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9,
    fontSize: "0.75rem", fontWeight: weight.semibold, whiteSpace: "nowrap" as const,
  };
  // A fork's world was rebuilt to the fork point. Say how far — a partial rebuild
  // is drivable but the annotator has to know they are at step `done`, not `total`,
  // or they will build the correction on top of a world that is short of where they
  // think it is.
  const rebuilt = restore
    ? restore.partial
      ? { label: `⚠ Rebuilt ${restore.done}/${restore.total}`, color: t.deltaAmber,
          title: `The branch was rebuilt to step ${restore.done} of ${restore.total}. ${restore.reason} You can still drive from here, but the world is short of the full prefix.` }
      : { label: `↺ Rebuilt to step ${restore.total}`, color: t.primary6,
          title: `The ${restore.total} step(s) before this fork were replayed for you — the world is at the fork point, ready to correct.` }
    : null;
  return (
    <span style={{ display: "inline-flex", gap: 6 }}>
      <span
        style={{ ...pill, color: preserved ? t.primary6 : t.n1 }}
        title={
          preserved
            ? "Your work in this world was kept — this is the same world you left."
            : isolated
              ? "This world was rebuilt from the task's seed state."
              : "Reset from the task's seed state (shared gym — not isolated to this attempt)."
        }
      >
        {preserved ? "● World kept" : "○ Fresh seed"}
      </span>
      {rebuilt && (
        <span style={{ ...pill, color: rebuilt.color }} title={rebuilt.title}>
          {rebuilt.label}
        </span>
      )}
      <span
        onClick={resetting ? undefined : onReset}
        title="Throw this world away and rebuild it to where this branch begins"
        style={{ ...pill, color: t.n1, cursor: resetting ? "default" : "pointer" }}
      >
        {resetting ? "Resetting…" : "⟲ Reset world"}
      </span>
    </span>
  );
}

/**
 * The workspace: the live gym on the left, the trajectory building itself on
 * the right.
 *
 * They are deliberately side by side rather than stacked. An annotator has to be
 * able to SEE their work being captured while they work — a lost interaction
 * discovered at the end of a task is an hour thrown away, and the whole point of
 * this screen is that the actions ARE the deliverable.
 */
export function ReviewSurface({ session, attemptId, owner, versionId, opening, error, onRetry }: {
  session: LiveSession | null;
  /** The review session — where the pane's recorded interactions land. */
  attemptId: string | null;
  /** The signed-in annotator. It MUST be the ticket's owner, or the live
   *  service closes the stream 4401 and the pane shows a dead surface. */
  owner?: string;
  /** Head version — the trajectory the annotator's steps land on. */
  versionId?: string | null;
  /** The gym is still being launched — a real Chromium, so seconds not ms. */
  opening?: boolean;
  /** Why it could not be opened. Shown INSTEAD of the pane: a live surface with
   *  no stream silently swallows every click and records nothing, which is worse
   *  than showing nothing at all. */
  error?: string | null;
  onRetry?: () => void;
}) {
  if (!session) return <GymPlaceholder opening={opening} error={error} onRetry={onRetry} />;
  return (
    <div style={{ display: "flex", minHeight: 0, flex: 1 }}>
      <div style={{ flex: 1, minWidth: 0, display: "flex" }}>
        <LiveBrowserPane
          attemptId={attemptId}
          session={session}
          owner={owner}
          apps={session?.apps ?? null}
        />
      </div>
      {attemptId && <LiveActionLog attemptId={attemptId} versionId={versionId ?? null} />}
    </div>
  );
}

/** What fills the workspace before the gym is up — or when it refused to start. */
function GymPlaceholder({ opening, error, onRetry }: { opening?: boolean; error?: string | null; onRetry?: () => void }) {
  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      gap: 10, border: `1px solid ${t.n7}`, borderRadius: t.radiusLg, background: t.n9, minHeight: 0,
    }}>
      {error ? (
        <>
          <Icon name="alert" size={20} color={t.redDark} />
          <div style={{ fontSize: "0.86rem", fontWeight: weight.semibold, color: t.n0 }}>The gym could not be opened</div>
          <div style={{ fontSize: "0.76rem", color: t.n2, maxWidth: 460, textAlign: "center", lineHeight: 1.5 }}>{error}</div>
          {onRetry && (
            <span onClick={onRetry} style={{ marginTop: 4, padding: "6px 14px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, fontSize: "0.75rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer" }}>
              Try again
            </span>
          )}
        </>
      ) : (
        <>
          <div style={{ fontSize: "0.86rem", fontWeight: weight.semibold, color: t.n0 }}>
            {opening ? "Opening the gym…" : "Preparing the workspace…"}
          </div>
          <div style={{ fontSize: "0.76rem", color: t.n2 }}>
            Seeding all five apps with this task's world.
          </div>
          <div style={{ marginTop: 4, width: 220, height: 4, background: t.n7, borderRadius: 3, overflow: "hidden" }}>
            <div style={{ height: "100%", width: "40%", background: t.primary6, borderRadius: 3, animation: "gymbar 1.1s ease-in-out infinite" }} />
          </div>
          <style>{"@keyframes gymbar{0%{margin-left:-40%}100%{margin-left:100%}}"}</style>
        </>
      )}
    </div>
  );
}

/** Polls the head version's steps while the annotator works. */
function LiveActionLog({ attemptId, versionId }: { attemptId: string; versionId: string | null }) {
  const [steps, setSteps] = useState<LoggedStep[]>([]);
  const [certifying, setCertifying] = useState(false);
  const [head, setHead] = useState<string | null>(versionId);

  // The head is server state — it moves on a fork or a select — so ask rather
  // than mirror it. It also does not exist until the annotator's first action,
  // which is why this keeps looking.
  useEffect(() => {
    if (versionId) { setHead(versionId); return; }
    let alive = true;
    const find = async () => {
      const id = await fetchHeadVersionId(attemptId);
      if (alive && id) setHead(id);
    };
    void find();
    const h = setInterval(() => { if (!head) void find(); }, 3000);
    return () => { alive = false; clearInterval(h); };
  }, [attemptId, versionId, head]);

  useEffect(() => {
    if (!head) return;
    let alive = true;
    const tick = async () => {
      const r = await fetchLiveSteps(attemptId, head);
      if (alive && r.ok) setSteps(r.steps);
    };
    void tick();
    // Steps are folded server-side after each event batch, so a short poll is
    // enough — and far simpler than a second socket that could disagree with the
    // one already carrying the interactions.
    const h = setInterval(() => void tick(), 1500);
    return () => { alive = false; clearInterval(h); };
  }, [attemptId, head]);

  return (
    <ActionLog
      steps={steps}
      certifying={certifying}
      onCertify={async () => {
        setCertifying(true);
        const r = await certifyTrajectory(attemptId, head ?? undefined);
        setCertifying(false);
        if (head) {
          const fresh = await fetchLiveSteps(attemptId, head);
          if (fresh.ok) setSteps(fresh.steps);
        }
        if (r.error) window.alert(`Could not check the trajectory: ${r.error}`);
      }}
    />
  );
}

/**
 * Which correction system an attempt is on.
 *
 * There is exactly one per attempt and it is decided by the DATA, never by a
 * preference: an attempt with TrajectoryVersion rows corrects through the
 * version graph, one without keeps the retired path it was started on. Rewriting
 * an in-flight attempt's history would be worse than carrying two code paths, so
 * nothing here migrates anything.
 *
 * "unknown" is a real third answer, not a placeholder. The lineage is read over
 * the network, and treating "not answered yet" as "no versions" is exactly how
 * the retired Submit gets painted onto a versioned attempt for as long as the
 * GET takes — which is the bug this whole change exists to remove.
 */
export type CorrectionPath = "unknown" | "versions" | "legacy";

export interface Lineage {
  path: CorrectionPath;
  /** The version the attempt would ship. Null until one is selected. */
  head: VersionNode | null;
}

const NO_LINEAGE_YET: Lineage = { path: "unknown", head: null };

/** Everything an annotator needs to work the version path without asking
 *  somebody, in the same words the buttons use (FORK_COPY), so the explanation
 *  cannot drift away from the controls it explains. */
function VersionPathGuide({ head }: { head: VersionNode | null }) {
  const v = head ? `v${head.versionNo}` : "the version you select";
  // Only the first letter: the fork hints are two sentences each, and lowercasing
  // the lot would restart the second one in lower case.
  const joined = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);
  const line = (n: string, text: string) => (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
      <span style={{ flexShrink: 0, fontFamily: t.fontMono, fontSize: "0.6875rem", fontWeight: weight.bold, color: t.primary6, width: 12 }}>{n}</span>
      <span style={{ fontSize: "0.719rem", lineHeight: 1.5, color: t.n2 }}>{text}</span>
    </div>
  );
  return (
    <div style={{ background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, padding: "12px 16px", display: "flex", flexDirection: "column", gap: 7 }}>
      <span style={{ fontSize: "0.8125rem", fontWeight: weight.bold, color: t.n1 }}>How this attempt is corrected and shipped</span>
      {line("1", "Read a version's steps and mark each one Verified or Wrong. A verdict sticks to the step, so it follows that step into every branch that keeps it.")}
      {line("2", `“${FORK_COPY.before.action}” ${joined(FORK_COPY.before.hint)} “${FORK_COPY.after.action}” ${joined(FORK_COPY.after.hint)} Either way the version you forked from is left exactly as it was.`)}
      {line("3", `Make the branch you want to ship the HEAD, then approve it. Head is the attempt's answer${head ? ` — right now that is ${v}` : " — nothing holds it yet"}.`)}
      {line("4", `Finalize (step 3 below) replays ${v} from a clean start, scores the saved verifier suite against the world that replay ends in, and freezes the three together as the sample. Only ${v} ships.`)}
    </div>
  );
}

/**
 * The attempt's version lineage, under the run it annotates.
 *
 * The graph re-reads itself after every fork and every compare-and-swap; both
 * hooks live here rather than in ReviewScreen so that traffic cannot re-render
 * the replay pane, whose surface is measured on each render. What DOES travel
 * upward is one small `Lineage` — the screen has to know which correction path
 * to render, and only this panel knows.
 */
export function LineagePanel({ sessionId, sessionSettled = true, isGym, onLineage }: {
  sessionId: string | null;
  /** Has the open ANSWERED? Not the same as whether it produced a session. While
   *  it is still in flight, "no session" must not be read as "no versions" — that
   *  paints the retired legacy controls onto a versioned attempt for the whole
   *  duration of the opening POST, which is the exact window this closes. */
  sessionSettled?: boolean;
  isGym: boolean;
  onLineage?: (l: Lineage) => void;
}) {
  const versions = useVersionGraph(sessionId);
  const steps = useVersionSteps(sessionId, versions.viewingId);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const viewing = versions.viewingId;

  // v1 is NOT baselined here any more. A gym attempt is human-do: its v1 is
  // minted empty by `ensure_manual_root` when the live browser opens, and the
  // annotator's own actions fill it. The old `versions.baseline()` call cloned
  // the breaker's canonical AGENT run into the attempt — 13 steps the annotator
  // never took — and, firing on `sessionId` while the multi-second live open was
  // still running, it won the race and became the head. That is the "agent run
  // reappeared" bug. The backend now refuses baseline for a human-do attempt too.

  // A SUCCESSFUL read of an empty lineage still hands back a graph object, so
  // `graph !== null` is exactly "the server has answered". Timing cannot say it:
  // `loading` goes true and false again inside a single React batch when the
  // response resolves in the same tick (measured — the flag never renders as
  // true), so a screen keyed off it would decide the attempt's correction path
  // from how fast the network happened to be.
  const graph = versions.graph;
  const path: CorrectionPath = !sessionId
    ? sessionSettled
      ? "legacy" // the open answered and there is no session — offline or a fixture
      : "unknown" // still opening; a guess here reinstates the retired path
    : graph === null
      ? "unknown" // unread — and a lineage we cannot read is not one to guess at
      : graph.versions.length > 0
        ? "versions"
        : isGym
          ? "unknown" // a gym attempt's v1 is being minted by the live open; wait for it — it never takes the legacy path
          : "legacy"; // a fixture, read and empty: nothing here migrates it

  const head = headOf(graph);
  useEffect(() => {
    onLineage?.({ path, head });
    // `head` is a fresh object on every graph read and reporting it re-renders
    // the parent; depending on the object (or on an inline callback) would spin.
    // The primitives below are what actually change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onLineage, path, head?.id, head?.status, head?.versionNo, head?.stepCount]);

  const viewed = versions.graph?.versions.find((v) => v.id === viewing) ?? null;
  const versionNos = Object.fromEntries((versions.graph?.versions ?? []).map((v) => [v.id, v.versionNo]));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {path === "versions" && <VersionPathGuide head={head} />}
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <div style={{ width: 340, flexShrink: 0 }}>
          <VersionGraph
            graph={versions.graph}
            viewingId={viewing}
            notice={versions.notice}
            busyVersionId={versions.busyVersionId}
            onView={versions.view}
            onSelectHead={versions.selectHead}
            onSetStatus={versions.setStatus}
            onDismissNotice={versions.dismissNotice}
          />
        </div>
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {steps.error && (
            <span style={{ fontSize: "0.72rem", color: t.redDark, fontWeight: weight.semibold }}>{steps.error}</span>
          )}
          <VersionSteps
            version={viewed}
            steps={steps.steps}
            loading={steps.loading}
            versionNos={versionNos}
            selectedStepId={selectedStepId}
            busyStepId={steps.busyStepId}
            onSelectStep={(id) => setSelectedStepId((cur) => (cur === id ? null : id))}
            onVerdict={steps.verdict}
            // Two named commands, never one mode argument: fork-before REJECTS the
            // step (it is absent from the child), fork-after KEEPS it. Transposing
            // them still yields a plausible-looking golden trajectory.
            onRejectStep={(s) => (viewing ? versions.fork(rejecting(viewing, s.stepId)) : undefined)}
            onContinueAfter={(s) => (viewing ? versions.fork(continuingAfter(viewing, s.stepId)) : undefined)}
          />
        </div>
      </div>
    </div>
  );
}

/** A dismissable failure line. Stacked rather than overlaid: a live-browser
 *  failure and a drive-forward failure are different facts and can coincide. */
function Toast({ message, onDismiss, bottom = 24 }: { message: string; onDismiss: () => void; bottom?: number }) {
  return (
    <div onClick={onDismiss} title="Dismiss"
      style={{ position: "fixed", left: "50%", bottom, transform: "translateX(-50%)", background: t.redLite, color: t.redDark, border: `1px solid color-mix(in srgb, ${t.red} 42%, ${t.n9})`, padding: "10px 16px", borderRadius: t.radiusLg, fontSize: "0.84rem", fontWeight: weight.semibold, zIndex: 70, cursor: "pointer", maxWidth: 540, boxShadow: t.shadowLg }}>
      {message}
    </div>
  );
}

const STATUS_LABEL: Record<string, string> = {
  draft: "Draft", steps_approved: "Steps approved", verifiers_generated: "Suite saved",
  benchmark_run: "Benchmarked", submitted: "Submitted",
};

function SaveBadge({ sessionId, status }: { sessionId: string | null; status: string }) {
  const saved = !!sessionId;
  const color = saved ? t.green : t.n3;
  return (
    <span title={saved ? "Your work autosaves to the platform database" : "Backend offline — changes are not being saved"}
      style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: "0.72rem", fontWeight: weight.semibold, color: t.n2 }}>
      <span style={{ width: 7, height: 7, borderRadius: t.radiusFull, background: color, boxShadow: saved ? `0 0 0 3px ${t.greenLite}` : "none" }} />
      {saved ? `Autosaved · ${STATUS_LABEL[status] ?? status}` : "Not saved (offline)"}
    </span>
  );
}

// --------------------------------------------------------------------------- shipping a version

/** What finalize hands back (backend/app/finalize.py:138-143). */
interface ShipResult {
  submissionId: string;
  versionId: string;
  reward: number;
  steps: number;
  replayed: boolean;
}

/** FastAPI's `detail` is a string on finalize's refusals but an OBJECT on the
 *  replay rejection (backend/app/api/versions.py:281-284). Rendering the object
 *  raw prints "[object Object]" over the one message that names the step which
 *  failed — the only part of it an annotator can act on. */
function refusalText(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; reason?: string; at?: number };
    const parts = [d.error, d.reason].filter(Boolean);
    // `at` indexes ACTIONS from zero; every step list an annotator reads is
    // numbered from one, so quoting it raw sends them to the wrong step.
    if (parts.length) return d.at != null ? `${parts.join(" — ")} (at step ${d.at + 1})` : parts.join(" — ");
  }
  return `Finalize failed (${status}) — nothing was shipped.`;
}

/**
 * Ship one version — POST /api/sessions/{id}/finalize (backend/app/api/versions.py:248).
 *
 * Deliberately not routed through lib/api's `post`, which collapses every
 * failure into null. Finalize's refusals ARE the product: "needs a QC-approved
 * version" and "does not replay cleanly, at step N" are two different jobs for
 * the annotator, and a null turns both into a button that appears to do nothing.
 */
async function shipVersion(
  sessionId: string,
  versionId: string,
  kind: string,
): Promise<{ ok: true; value: ShipResult } | { ok: false; message: string }> {
  let res: Response;
  try {
    res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/finalize`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ versionId, kind }),
    });
  } catch {
    return { ok: false, message: "Could not reach the server — nothing was shipped." };
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) return { ok: false, message: refusalText(body, res.status) };
  return { ok: true, value: body as ShipResult };
}

/**
 * The one way an attempt with a version graph ships.
 *
 * It replaces "Approve & submit to dataset", which built its golden from the
 * recorded run and could therefore ship a step the annotator had rejected. The
 * copy names the version, the replay and the freeze, because "Finalize v2" means
 * nothing to somebody who has never read the schema — and every reason it cannot
 * run yet is written as the thing to go and do.
 */
function FinalizeDock({ sessionId, head, benchmarkRun, kind, alreadyShipped, onShipped }: {
  sessionId: string | null;
  head: VersionNode | null;
  /** The benchmark has been run. Doubles as "a suite exists server-side":
   *  `runBenchmark` SAVES the suite before scoring it, and finalize refuses
   *  (409) an attempt with no suite to score against. */
  benchmarkRun: boolean;
  kind: string;
  alreadyShipped: boolean;
  onShipped: (r: ShipResult) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shipped, setShipped] = useState<ShipResult | null>(null);

  const v = head ? `v${head.versionNo}` : "this version";
  const blocker = !sessionId
    ? "This attempt was never saved — the backend is offline, so there is nothing to ship."
    : !head
      ? "No version is the head yet. In Version lineage above, open the branch you want to ship and press “Make it the head”."
      : head.status !== "approved"
        ? `${v} is the head, but nobody has approved it. Approve it in Version lineage above — finalize refuses a version no reviewer signed off.`
        : !benchmarkRun
          ? "No suite has been scored yet. Run the benchmark in step 2 above — that is what saves the suite finalize scores against."
          : null;

  const shell = { background: t.n9, border: `1px solid ${t.n7}`, borderRadius: t.radiusXl, boxShadow: t.shadowMd, padding: "16px 22px", display: "flex", flexDirection: "column" as const, gap: 12 };

  if (shipped || alreadyShipped) {
    return (
      <div style={shell}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: "0.875rem", fontWeight: weight.bold, color: t.greenDark }}>
          <Icon name="check" size={16} stroke={2.4} color={t.greenDark} /> Shipped — this attempt is frozen and can no longer be edited.
        </span>
        {shipped && (
          <span style={{ fontSize: "0.78rem", lineHeight: 1.5, color: t.n2 }}>
            Sample <span style={{ fontFamily: t.fontMono, fontSize: "0.74rem" }}>{shipped.submissionId}</span> · {shipped.steps} step{shipped.steps === 1 ? "" : "s"} · reward {shipped.reward} ·{" "}
            {shipped.replayed ? "replayed from a clean start before scoring" : "scored without a replay"}.
          </span>
        )}
      </div>
    );
  }

  return (
    <div style={shell}>
      <span style={{ fontSize: "0.875rem", fontWeight: weight.bold, color: t.n0 }}>Ship {v} as this attempt's sample</span>
      <span style={{ fontSize: "0.8rem", lineHeight: 1.55, color: t.n2, maxWidth: 720 }}>
        Finalizing replays {v}&apos;s {head?.stepCount ?? 0} step{(head?.stepCount ?? 0) === 1 ? "" : "s"} from a clean start — not from a saved checkpoint — scores the saved verifier
        suite against the world that replay ends in, and freezes the trajectory, the suite and the score together. Only {v} ships: steps you
        rejected are not in it, and no other branch is included.
      </span>
      {blocker ? (
        <span style={{ fontSize: "0.78rem", lineHeight: 1.5, fontWeight: weight.semibold, color: t.n1, background: tint(t.yellow, 14), padding: "9px 12px", borderRadius: t.radiusLg }}>{blocker}</span>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <Button
            variant="primary"
            disabled={busy}
            style={{ minHeight: 44 }}
            onClick={async () => {
              if (!sessionId || !head) return;
              setBusy(true);
              setError(null);
              const res = await shipVersion(sessionId, head.id, kind);
              setBusy(false);
              if (!res.ok) {
                setError(res.message);
                return;
              }
              setShipped(res.value);
              onShipped(res.value);
            }}
          >
            {busy ? `Replaying ${v}…` : `Replay ${v} and ship it as the sample`}
          </Button>
          <span style={{ fontSize: "0.74rem", lineHeight: 1.5, color: t.n3, maxWidth: 420 }}>
            Produces one frozen sample: the golden trajectory, the suite version it was scored with, the reward that scoring gave it, and the
            world it ended on. Nothing about it can be edited afterwards.
          </span>
        </div>
      )}
      {error && (
        <span style={{ fontSize: "0.78rem", lineHeight: 1.5, fontWeight: weight.semibold, color: t.redDark }}>{error}</span>
      )}
    </div>
  );
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <div style={{ width: 1440, margin: "0 auto", minHeight: "100vh", display: "flex", flexDirection: "column", background: t.n85, border: `1px solid ${t.n7}` }}>
      {children}
    </div>
  );
}

interface TaskNav {
  index: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
  onSkip: () => void;
  onBrowseGym: () => void;
  gymTaskId?: string | null;
  onExitGym?: () => void;
  onOpenQa: () => void;
  annotator: Annotator | null;
  onOpenProfile: () => void;
  queueSet?: "breakers" | "fixtures";
  onToggleQueue?: () => void;
  gymAdhoc?: boolean;
  /** Back to the My-tasks board. Present only when the screen was opened from it. */
  onBackToTasks?: () => void;
  // Editing the prompt re-drives the WHOLE run from the initial state under the
  // new instruction (gym tasks only), then a fresh review of that run.
  onPromptRerun?: (prompt: string) => Promise<void>;
}

export function ReviewScreen({ data, nav, startFresh, onStartNew }: { data: ReviewData; nav: TaskNav; startFresh: boolean; onStartNew: () => void }) {
  const [state, dispatch] = useReducer(reducer, data, makeInitialState);
  const [sessionId, setSessionId] = useState<string | null>(null);
  // Whether the open has ANSWERED, which is not the same as whether it produced a
  // session. Without this, "no session yet" is indistinguishable from "no session
  // exists", and the retired legacy controls paint onto a versioned attempt for
  // the whole duration of the opening POST — the precise window this retirement
  // exists to close.
  const [sessionSettled, setSessionSettled] = useState(false);
  // Which correction system this attempt is on, reported by the lineage panel.
  // It starts UNKNOWN and no correction UI of either kind renders until it is
  // answered: guessing "legacy" would show the retired Submit on a versioned
  // attempt, which is the exact failure being retired here.
  const [lineage, setLineage] = useState<Lineage>(NO_LINEAGE_YET);
  const versioned = lineage.path === "versions";
  const legacy = lineage.path === "legacy";
  const [promptOverride, setPromptOverride] = useState<string | null>(null);
  const [driving, setDriving] = useState<null | "queued" | "running">(null);
  const [driveError, setDriveError] = useState<string | null>(null);
  const [autogen, setAutogen] = useState<null | "queued" | "running">(null);
  const [autogenResult, setAutogenResult] = useState<AutogenResult | null>(null);
  const [editingState, setEditingState] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  // The LIVE resume context. Each drive-forward returns the world it ended in, and
  // we adopt it — so successive corrections COMPOUND (round N+1 continues from where
  // round N got to) instead of re-anchoring to the original run's end-state. That's
  // what lets an annotator iteratively steer the agent to the target.
  const [liveResume] = useState(data.gymResume);
  const [liveSession, setLiveSession] = useState<LiveSession | null>(null);
  const [resettingWorld, setResettingWorld] = useState(false);
  const [liveOpening, setLiveOpening] = useState(false);
  const [liveNotice, setLiveNotice] = useState<string | null>(null);
  // Why the gym would not start. Distinct from `liveNotice` (a transient toast):
  // this one REPLACES the workspace, because a live pane with no stream accepts
  // clicks and records none of them.
  const [liveError, setLiveError] = useState<string | null>(null);
  // Whether a browser is open server-side for this attempt. A ref, not state:
  // the unmount cleanup below reads it after the last render, and it is also set
  // by the re-attach probe, which must not repaint the pane.
  const liveOpenRef = useRef(false);

  useEffect(() => {
    if (!state.playing) return;
    const id = setInterval(() => dispatch({ t: "tick" }), 1100);
    return () => clearInterval(id);
  }, [state.playing]);

  // ---- M4 persistence: open/resume the session, then mirror each committed
  // transition to the backend so the annotator's work survives a refresh.
  const statusRef = useRef<string>("draft");
  const suiteSigRef = useRef<string>("");
  const submittedRef = useRef(false);
  const rerunRef = useRef<number | null>(null);
  const reviewedRef = useRef<number>(-1);

  useEffect(() => {
    let alive = true;
    openSession(data.task.id, { fresh: startFresh }).then((snap) => {
      if (!alive) return;
      setSessionSettled(true);
      if (!snap) return;
      setSessionId(snap.sessionId);
      const results = (snap.lastBenchmark?.results as Record<string, string>) ?? {};
      // Reconstruct the annotator's AUTHORED suite from the persisted latest
      // version: verifiers the human added, plus edits to generated ones. Without
      // this the suite silently reverts to the generated set on reload (and the
      // next Run would overwrite the DB with the reverted suite).
      const origById = new Map(data.verifiers.map((v) => [v.id, v]));
      const added: Verifier[] = [];
      const edits: Record<string, { assertion: string; code: string }> = {};
      for (const pv of snap.suite?.verifiers ?? []) {
        const orig = origById.get(pv.id);
        if (!orig || pv.addedByHuman) {
          added.push({ id: pv.id, level: pv.level as Verifier["level"], assertion: pv.assertion, code: pv.code, check: pv.check ?? undefined, placeholder: pv.placeholder, failsUntilCorrected: pv.failsUntilCorrected });
        } else if (pv.assertion !== orig.assertion || pv.code !== orig.code) {
          edits[pv.id] = { assertion: pv.assertion, code: pv.code };
        }
      }
      // Human attestations (overrides) from the last run — so reward can't drop
      // 1->0 when the next run silently omits them.
      const overrides: Record<string, boolean> = {};
      for (const id of snap.lastBenchmark?.overridden ?? []) overrides[id] = true;
      // The persisted correction branch — restores the fork's exact steps/count.
      const branchTail = snap.branch ? snap.branch.steps : null;
      const rerunMode = snap.branch ? snap.branch.mode : null;
      const hydrateAction = {
        t: "hydrate" as const,
        status: snap.status,
        rerunFrom: snap.rerunFrom,
        reviewedThrough: snap.reviewedThrough,
        results,
        branchTail,
        rerunMode,
        added,
        edits,
        overrides,
        submission: snap.submission ? { reward: snap.submission.reward, kind: snap.submission.kind } : null,
      };
      const restored = reducer(makeInitialState(data), hydrateAction);
      // Seed the sync refs to the RESTORED state so we don't echo it back.
      statusRef.current = snap.status;
      rerunRef.current = snap.rerunFrom;
      reviewedRef.current = restored.verifiedThrough;
      submittedRef.current = snap.status === "submitted";
      suiteSigRef.current = restored.verifiersGenerated
        ? JSON.stringify(verifierPayloads(restored))
        : "";
      if (snap.status !== "draft" || snap.rerunFrom != null || snap.reviewedThrough > 0 || snap.suite != null || snap.branch != null) {
        dispatch(hydrateAction);
      }
    });
    return () => {
      alive = false;
    };
  }, [data.task.id]);

  // ---- the live browser -----------------------------------------------------
  // A reload never runs the cleanup below, so the browser from the previous
  // mount is still running. Find it, so leaving the task reclaims it and the
  // next attach re-tickets THAT browser instead of starting a second one.
  useEffect(() => {
    if (!sessionId) return;
    let alive = true;
    void currentLiveBrowser(sessionId).then((res) => {
      if (alive && res.ok && res.value) liveOpenRef.current = true;
    });
    return () => { alive = false; };
  }, [sessionId]);

  // A live browser is a real Chromium on the host. Leaving the task without
  // closing it leaks one per task the annotator opened, until the box is out of
  // memory — which is why this runs whether or not the pane is on screen.
  useEffect(() => {
    if (!sessionId) return;
    return () => {
      if (!liveOpenRef.current) return;
      liveOpenRef.current = false;
      void closeLiveBrowser(sessionId);
    };
  }, [sessionId]);

  const showLive = async () => {
    if (!sessionId) {
      setLiveError("The gym needs a saved session, and the backend did not return one.");
      return;
    }
    setLiveError(null);
    setLiveOpening(true);
    // Claim it BEFORE the request, not after. Opening launches a real Chromium
    // and takes seconds; an annotator who hits Next while it still reads
    // "Opening…" unmounts this component, and a flag set only on success leaves
    // the cleanup with nothing to close while the server goes on to create a
    // browser nobody holds. A close for a session that was never opened is
    // idempotent and free; a leaked Chromium is neither.
    liveOpenRef.current = true;
    // Always mint here, even when the probe above found a session: tickets
    // expire, and a socket opened with a stale one closes 4401 and streams
    // nothing at all.
    const res = await attachLiveBrowser(sessionId);
    setLiveOpening(false);
    if (!res.ok) {
      // The request failed, but the server may still have got as far as starting
      // a browser — a timeout looks exactly like this from here — so reclaim it
      // rather than assume nothing happened.
      liveOpenRef.current = false;
      void closeLiveBrowser(sessionId);
      // Say so where the gym would have been. There is no replay to fall back
      // to any more, and a live pane with no stream is a surface that swallows
      // every click and reports nothing.
      setLiveError(res.message);
      return;
    }
    setLiveSession(res.value);
    setLiveNotice(null);
    setLiveError(null);
  };

  // Open the gym as soon as the attempt exists. The annotator's job on this
  // screen is to DO the task, so the workspace is what they must land on — the
  // old flow made it a second, deliberate click because the recorded run was the
  // thing being reviewed.
  //
  // Fires once per attempt, tracked by its OWN ref rather than `liveOpenRef`:
  // that ref is set asynchronously by the reload probe above, and on a genuine
  // reload we still WANT the attach to run (it re-tickets the surviving browser
  // — an expired ticket closes the socket 4401). Attach against an existing
  // browser is safe, so the worst case is one idempotent re-ticket, not a second
  // Chromium.
  const autoOpenedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!sessionId || liveSession) return;
    if (autoOpenedRef.current === sessionId) return;
    autoOpenedRef.current = sessionId;
    void showLive();
    // showLive closes over sessionId, which is the dependency that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, liveSession]);

  const resetWorld = async () => {
    if (!sessionId || resettingWorld) return;
    // Destructive and not undoable — the whole point is that it discards work.
    if (!window.confirm("Discard your changes to this world and rebuild it to where this branch begins?")) return;
    setResettingWorld(true);
    const res = await resetLiveWorld(sessionId);
    setResettingWorld(false);
    if (!res.ok) {
      setLiveNotice(res.message);
      return;
    }
    // Take the server's own account of the rebuilt world rather than restating it.
    // On a fork the reset replays the prefix again, so `restore` describes how far
    // the world was taken — dropping it would leave the badge showing stale progress.
    setLiveSession((prev) =>
      prev ? { ...prev, world: res.value.world as LiveSession["world"], restore: res.value.restore } : prev,
    );
  };

  // Run the verifier suite through the backend execution engine (M5). Falls
  // back to a flag-derived result only when the backend is unreachable.
  const runBenchmark = async () => {
    // Gym tasks carry the real milestone verdict already (verifierState/reward read
    // v.gymResult / data.gymReward). Still persist a benchmark run server-side —
    // scored from the authoritative gym-engine verdict — so the session reaches
    // benchmark_run and the sample becomes SUBMITTABLE (submit requires a run).
    if (data.source === "gym") {
      const overrides = Object.keys(state.overrides);
      const corrected = state.rerunFrom != null;
      if (sessionId) {
        await saveSuite(sessionId, verifierPayloads(state)); // persist the milestones as the human suite
        const out = await runVerifiers(sessionId, { corrected, verifiers: verifierPayloads(state), overrides });
        dispatch({ t: "benchmarkComplete", results: out?.results ?? {}, reward: out?.reward ?? null });
        return;
      }
      dispatch({ t: "benchmarkComplete", results: {} }); // offline — reveal only
      return;
    }
    const verifiers = verifierPayloads(state);
    const overrides = Object.keys(state.overrides);
    const corrected = state.rerunFrom != null;
    if (sessionId) {
      // Persist the current suite first — the server scores the PERSISTED suite,
      // not this request's list, so the stored reward is authoritative.
      await saveSuite(sessionId, verifiers);
      const out = await runVerifiers(sessionId, { corrected, verifiers, overrides });
      if (out) {
        dispatch({ t: "benchmarkComplete", results: out.results, reward: out.reward ?? null });
        return;
      }
    }
    dispatch({ t: "benchmarkComplete", results: offlineResults(state) });
  };

  // Gate-status transitions (draft → steps_approved → verifiers_generated).
  const status = sessionStatus(state);
  useEffect(() => {
    if (!sessionId || status === statusRef.current) return;
    statusRef.current = status;
    if (status === "steps_approved" || status === "verifiers_generated") {
      void patchSession(sessionId, { status });
    }
  }, [sessionId, status]);

  // Correction fork — persist the re-run point AND the re-lock together. A
  // correction re-locks Section 2 to 'draft'; writing both atomically means a
  // failed /rerun can't leave the DB status contradicting the persisted fork
  // (which would reload with Section 2 wrongly unlocked / submittable).
  useEffect(() => {
    if (!sessionId || state.rerunFrom === rerunRef.current) return;
    rerunRef.current = state.rerunFrom;
    if (state.rerunFrom != null) {
      statusRef.current = "draft"; // keep the status effect from firing a duplicate PATCH
      void patchSession(sessionId, { rerunFrom: state.rerunFrom, status: "draft" });
    }
  }, [sessionId, state.rerunFrom]);

  // Granular review progress — persist every verify/approve so it survives a
  // refresh (each click reflected in the DB).
  useEffect(() => {
    if (!sessionId || state.verifiedThrough === reviewedRef.current) return;
    reviewedRef.current = state.verifiedThrough;
    void patchSession(sessionId, { reviewedThrough: state.verifiedThrough });
  }, [sessionId, state.verifiedThrough]);

  // Verifier suite — save a new immutable version whenever it changes.
  const suiteSig = state.verifiersGenerated ? JSON.stringify(verifierPayloads(state)) : "";
  useEffect(() => {
    if (!sessionId || !suiteSig || suiteSig === suiteSigRef.current) return;
    suiteSigRef.current = suiteSig;
    void saveSuite(sessionId, verifierPayloads(state));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, suiteSig]);

  // Submission — await the server, reconcile from its snapshot, surface failures.
  // Never optimistically show "submitted"; the server is authoritative.
  const handleSubmit = async () => {
    if (!canSubmit(state) || submittedRef.current) return;
    if (!sessionId) { dispatch({ t: "submitFailed", error: "Not saved — the backend is offline." }); return; }
    submittedRef.current = true;
    const snap = await submitSession(sessionId, {
      reward: reward(state) ?? 0,
      override: Object.keys(state.overrides).length > 0,
      kind: reward(state) === 1 ? "golden" : "breaker",
    });
    if (snap?.submission) {
      dispatch({ t: "submitConfirmed", reward: snap.submission.reward, kind: snap.submission.kind });
    } else {
      submittedRef.current = false; // allow retry
      dispatch({ t: "submitFailed", error: "Submit failed — nothing was saved. Check the connection and retry." });
    }
  };

  const steps = visibleSteps(state);

  const onAddVerifier = (assertion: string, code: string) => {
    const placeholder = !code.trim() || code.includes("/* define check */");
    const v: Verifier = { id: `add-${state.added.length + 1}`, level: state.activeLevel, assertion, code, placeholder };
    dispatch({ t: "addVerifier", verifier: v });
  };

  return (
    <Frame>
      <Header {...nav} />
      {driving && <GymLoading taskId={data.task.id} phase={driving} />}
      {driveError && <Toast message={driveError} onDismiss={() => setDriveError(null)} />}
      {liveNotice && <Toast message={liveNotice} onDismiss={() => setLiveNotice(null)} bottom={driveError ? 82 : 24} />}
      <div style={{ padding: "16px 16px 8px" }}>
        <SectionHeader n={1} title="Do the task" subtitle="Work through it in the live gym. Every action you take is recorded as the trajectory." right={
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {liveSession && (
              <WorldBadge
                world={liveSession?.world}
                isolated={liveSession?.isolated}
                restore={liveSession?.restore}
                onReset={() => void resetWorld()}
                resetting={resettingWorld}
              />
            )}
            <SaveBadge sessionId={sessionId} status={status} />
            {/* Every control below writes (or replays) a legacy correction
                branch, which no version can contain and finalize cannot ship.
                On a versioned attempt they are absent rather than disabled: an
                annotator who can see a control they must not use still has to
                ask somebody why. */}
            {legacy && sessionId && state.rerunFrom != null && (
              <span onClick={() => setShowHistory(true)} title="Every correction round you made on this task"
                style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9, color: t.n1, fontSize: "0.75rem", fontWeight: weight.semibold, cursor: "pointer", whiteSpace: "nowrap" }}>
                ⟲ Iterations
              </span>
            )}
            {legacy && data.source === "gym" && data.gymResume && (
              <span onClick={() => setEditingState(true)} title="Edit the world state and re-verify against the gym"
                style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9, color: t.primary6, fontSize: "0.75rem", fontWeight: weight.semibold, cursor: "pointer", whiteSpace: "nowrap" }}>
                ✎ Edit state
              </span>
            )}
            {legacy && data.source === "gym" && data.gymResume && (
              <span
                onClick={driving ? undefined : async () => {
                  // Continue the task from where it stopped: drive the live agent
                  // forward from the final state and fork on the new steps.
                  setDriveError(null);
                  const fromStep = steps.length;
                  setDriving("queued");
                  const res = await driveForwardGym(
                    { taskId: data.task.id, seed: data.gymResume!.seed, worldState: data.gymResume!.worldState, resumeUrl: data.gymResume!.finalUrl || "/", resumeStep: fromStep, agent: "openai", sessionId: sessionId ?? undefined },
                    { onStatus: (s) => setDriving(s === "done" || s === "error" ? null : s) },
                  );
                  setDriving(null);
                  if (res && res.steps.length) {
                    const branch = res.steps.map((s, i) => ({ ...s, idx: fromStep + i + 1 }));
                    if (sessionId) await rerunGymBranch(sessionId, { fromStep, steps: branch, mode: "agent" });
                    dispatch({ t: "correctAndRerun", fromStep, branch, mode: "agent", gymReward: res.reward });
                  } else {
                    setDriveError("The live agent couldn't continue — the gym may be unreachable or the model unavailable.");
                  }
                }}
                title="Load the corrected state and let a live agent (gpt-5.1) continue the task in the gym (slow)"
                style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9, color: driving ? t.n3 : t.primary6, fontSize: "0.75rem", fontWeight: weight.semibold, cursor: driving ? "default" : "pointer", whiteSpace: "nowrap" }}>
                {driving ? (driving === "queued" ? "Queued…" : "Agent driving…") : "⚡ Drive forward (live agent)"}
              </span>
            )}
            {(state.submitted || status === "submitted") && (
              <span onClick={onStartNew} title="This session is submitted and locked — start a fresh annotation of this task"
                style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "5px 11px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9, color: t.primary6, fontSize: "0.75rem", fontWeight: weight.semibold, cursor: "pointer", whiteSpace: "nowrap" }}>
                <Icon name="plus" size={13} stroke={2.2} /> New annotation
              </span>
            )}
          </div>
        } />
        {/* Section 1 fills exactly the first screen: viewport minus the header
            (56) + this block's padding (16+8) + the section header (~36) + a small
            buffer. Getting this wrong makes the page scroll, sliding the replay's
            tab-strip/URL bar up under the sticky header (clipping). minHeight kept
            modest so short viewports degrade gracefully instead of forcing overflow. */}
        <div style={{ display: "flex", gap: 16, height: "calc(100dvh - 134px)", minHeight: 440 }}>
          <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
            <ReviewSurface
              session={liveSession}
              attemptId={sessionId}
              owner={nav.annotator?.email}
              opening={liveOpening}
              error={liveError}
              onRetry={() => void showLive()}
            />
          </main>
          <RightPanel
            task={promptOverride ? { ...data.task, prompt: promptOverride } : data.task}
            summary={runSummary(state)}
            // Gym: saving a new prompt re-drives the WHOLE run under it (then a
            // fresh review). Fixtures: just override the displayed prompt.
            // Editing the brief no longer re-drives a model: the annotator does
            // the task themselves, so a prompt edit is just a prompt edit. It
            // used to throw away the attempt and run a fresh stochastic agent.
            onSavePrompt={setPromptOverride}
            rerunsOnSave={false}
          />
        </div>
        {/* The lineage of THIS run: v1, every correction hanging off it, and the
            per-step verdicts. It sits with the trace it describes rather than
            behind a modal — deciding which version is the attempt's answer is
            part of reviewing the run, not a separate errand. */}
        <div style={{ padding: "16px 16px 0", display: "flex", flexDirection: "column", gap: 12 }}>
          {/* Rounds recorded on the retired path before this attempt was
              versioned. They belong to no version, so finalize cannot ship them
              — saying so is the only honest thing to do with work that is
              already in the database and cannot be migrated. */}
          {versioned && state.rerunFrom != null && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 14px", background: tint(t.yellow, 14), border: `1px solid ${t.n7}`, borderRadius: t.radiusLg }}>
              <Icon name="alert" size={14} color={t.yellowDark} style={{ flexShrink: 0 }} />
              <span style={{ flex: 1, fontSize: "0.75rem", lineHeight: 1.5, color: t.n1 }}>
                This attempt also has correction rounds recorded on the retired path, from step {state.rerunFrom}. They are part of no
                version, so finalizing will not ship them — the lineage below is what ships.
              </span>
              {sessionId && (
                <span onClick={() => setShowHistory(true)} style={{ flexShrink: 0, fontSize: "0.72rem", fontWeight: weight.semibold, color: t.primary6, cursor: "pointer", whiteSpace: "nowrap" }}>
                  Read those rounds
                </span>
              )}
            </div>
          )}
          <LineagePanel sessionId={sessionId} sessionSettled={sessionSettled} isGym={data.source === "gym"} onLineage={setLineage} />
        </div>
      </div>

      <div style={{ padding: "8px 16px 24px" }}>
        <SectionHeader n={2} title="Build the verifier suite" subtitle="Generate multi-level verifiers, edit them, then run the benchmark. Reward = 1 requires every verifier to pass." done={state.submitted} right={
          data.source === "gym" ? (
            <span
              onClick={autogen ? undefined : async () => {
                setAutogenResult(null);
                setAutogen("queued");
                const res = await autogenVerifiers(data.task.id, 0, { onStatus: (s) => setAutogen(s === "done" || s === "error" ? null : s) });
                setAutogen(null);
                setAutogenResult(res);
              }}
              title="Autonomous reward agent: generate a verifier suite and validate it with the oracle gate (0 on initial, 1 on golden)"
              style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "6px 12px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n9, color: autogen ? t.n3 : t.primary6, fontSize: "0.75rem", fontWeight: weight.semibold, cursor: autogen ? "default" : "pointer", whiteSpace: "nowrap" }}>
              {autogen ? (autogen === "queued" ? "Reward agent queued…" : "Generating + validating…") : "🤖 Auto-generate verifiers"}
            </span>
          ) : undefined
        } />
        <VerifierSuite
          state={state}
          onGenerate={() => dispatch({ t: "generate" })}
          onSetLevel={(l) => dispatch({ t: "setLevel", level: l })}
          onAddVerifier={onAddVerifier}
          onEditVerifier={(id, assertion, code) => dispatch({ t: "editVerifier", id, assertion, code })}
          onOverride={(id) => dispatch({ t: "override", id })}
          onRun={runBenchmark}
          // The legacy submit ships the RECORDED run plus a branch, so on a
          // versioned attempt it could ship a step the annotator rejected. It is
          // not offered there; step 3 below is.
          onSubmit={legacy ? handleSubmit : undefined}
          submitNote={
            versioned
              ? "This attempt has a version lineage — ship it in step 3 below, which binds the version, this suite and the score together."
              : lineage.path === "unknown"
                ? "Reading this attempt's version lineage…"
                : undefined
          }
        />
      </div>

      {versioned && (
        <div style={{ padding: "8px 16px 24px" }}>
          <SectionHeader n={3} title="Ship the approved version" subtitle="Replays it from a clean start, scores the saved suite, and freezes all three together." done={state.submitted} />
          <FinalizeDock
            sessionId={sessionId}
            head={lineage.head}
            benchmarkRun={state.benchmarkRun}
            kind={reward(state) === 1 ? "golden" : "breaker"}
            alreadyShipped={state.submitted || status === "submitted"}
            onShipped={(r) => {
              // The server has frozen the sample; mirror that into the screen so
              // the rest of it locks the same way a legacy submit locks it.
              submittedRef.current = true;
              dispatch({ t: "submitConfirmed", reward: r.reward, kind: r.reward === 1 ? "golden" : "breaker" });
            }}
          />
        </div>
      )}

      {autogenResult && <AutogenPanel result={autogenResult} onClose={() => setAutogenResult(null)} />}
      {showHistory && sessionId && <IterationHistory sessionId={sessionId} onClose={() => setShowHistory(false)} />}
      {editingState && data.source === "gym" && data.gymResume && (
        <StateEditor
          world={(liveResume ?? data.gymResume).worldState ?? {}}
          onClose={() => setEditingState(false)}
          onApply={async (edits) => {
            const res = await resumeGymReview({
              taskId: data.task.id,
              seed: data.gymResume!.seed,
              worldState: data.gymResume!.worldState,
              urlTrail: data.gymResume!.urlTrail,
              finalUrl: data.gymResume!.finalUrl,
              edits,
            });
            if (res) dispatch({ t: "gymResumed", reward: res.reward });
            setEditingState(false);
          }}
        />
      )}
    </Frame>
  );
}

/** The iteration history: every correction ROUND the annotator made on this
 *  session, oldest → newest. The main view only restores the latest round, so this
 *  is how you step back through how the agent was steered toward the target. */
function IterationHistory({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalA11y(onClose, dialogRef);
  const [rounds, setRounds] = useState<HistoryRound[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { void fetchSessionHistory(sessionId).then(setRounds); }, [sessionId]);
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 56 }}>
      <div ref={dialogRef} {...DIALOG} aria-label="Iteration history" onClick={(e) => e.stopPropagation()}
           style={{ width: 660, maxHeight: "82vh", background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, display: "flex", flexDirection: "column", overflow: "hidden", outline: "none" }}>
        <div style={{ padding: "18px 22px 14px", borderBottom: `1px solid ${t.n7}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>⟲ Iteration history</div>
            <div style={{ marginTop: 3, fontSize: "0.8rem", color: t.n2 }}>
              Every correction round on this task — each kept its own fork point, instruction and steps.
            </div>
          </div>
          <span onClick={onClose} style={{ cursor: "pointer", color: t.n3, display: "inline-flex" }}><Icon name="close" size={18} /></span>
        </div>
        <div style={{ overflowY: "auto", padding: "6px 0" }}>
          {rounds === null && <div style={{ padding: "22px", fontSize: "0.84rem", color: t.n3 }}>Loading…</div>}
          {rounds?.length === 0 && (
            <div style={{ padding: "22px", fontSize: "0.84rem", color: t.n3 }}>
              No corrections yet — correct a step to start iterating.
            </div>
          )}
          {rounds?.map((r) => {
            const isOpen = open === r.branchId;
            return (
              <div key={r.branchId} style={{ borderBottom: `1px solid ${t.n8}` }}>
                <div onClick={() => setOpen(isOpen ? null : r.branchId)}
                     style={{ padding: "12px 22px", cursor: "pointer", display: "flex", alignItems: "flex-start", gap: 12 }}>
                  <span style={{ flexShrink: 0, width: 26, height: 26, borderRadius: t.radiusFull, background: t.primary6, color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: "0.72rem", fontWeight: weight.bold }}>{r.round}</span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: "0.84rem", fontWeight: weight.semibold, color: t.n0 }}>
                      Corrected step {r.fromStep} → {r.stepCount} new step{r.stepCount === 1 ? "" : "s"}
                    </div>
                    {r.correction ? (
                      <div style={{ marginTop: 3, fontSize: "0.79rem", color: t.n2, fontStyle: "italic" }}>“{r.correction}”</div>
                    ) : (
                      <div style={{ marginTop: 3, fontSize: "0.76rem", color: t.n3 }}>(no instruction recorded)</div>
                    )}
                    <div style={{ marginTop: 4, fontSize: "0.72rem", color: t.n3, fontFamily: t.fontMono }}>
                      {r.mode} · {new Date(r.at).toLocaleString()}
                    </div>
                  </div>
                  <span style={{ flexShrink: 0, color: t.n3, fontSize: "0.72rem" }}>{isOpen ? "▲" : "▼"}</span>
                </div>
                {isOpen && (
                  <div style={{ padding: "2px 22px 14px 60px", display: "flex", flexDirection: "column", gap: 6 }}>
                    {r.steps.map((st) => (
                      <div key={st.idx} style={{ display: "flex", alignItems: "center", gap: 9, fontSize: "0.79rem", color: t.n1 }}>
                        <span style={{ fontFamily: t.fontMono, color: t.n3, width: 22, flexShrink: 0 }}>{String(st.idx).padStart(2, "0")}</span>
                        <span style={{ fontSize: "0.63rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.04em", color: ACTION_COLOR[st.type], width: 62, flexShrink: 0 }}>{st.type}</span>
                        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{st.description}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function StateEditor({ world, onClose, onApply }: { world: Record<string, unknown>; onClose: () => void; onApply: (edits: Record<string, unknown>) => Promise<void> }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalA11y(onClose, dialogRef);
  const shop = ((world?.shop ?? {}) as Record<string, unknown>);
  const cart = ((shop.cart ?? {}) as Record<string, unknown>);
  const nOrders = Object.keys((shop.orders ?? {}) as object).length;
  const nCart = ((cart.items ?? []) as unknown[]).length;
  const nReturns = Object.keys((shop.returns ?? {}) as object).length;
  const nSubs = Object.keys((shop.subscriptions ?? {}) as object).length;
  const [user, setUser] = useState<string>((shop.current_user_id as string) ?? "");
  const [promo, setPromo] = useState<string>((cart.applied_promo as string) ?? "");
  const [voidOrders, setVoidOrders] = useState(false);
  const [emptyCart, setEmptyCart] = useState(false);
  const [voidReturns, setVoidReturns] = useState(false);
  const [voidSubs, setVoidSubs] = useState(false);
  const [busy, setBusy] = useState(false);

  const build = (): Record<string, unknown> => {
    const e: Record<string, unknown> = {};
    if (((shop.current_user_id as string) ?? "") !== user) e["shop.current_user_id"] = user || null;
    if (((cart.applied_promo as string) ?? "") !== promo) e["shop.cart.applied_promo"] = promo || null;
    if (voidOrders) e["shop.orders"] = {};
    if (emptyCart) e["shop.cart.items"] = [];
    if (voidReturns) e["shop.returns"] = {};
    if (voidSubs) e["shop.subscriptions"] = {};
    return e;
  };
  const edits = build();
  const field = { display: "block", marginTop: 5, width: "100%", boxSizing: "border-box" as const, padding: "8px 11px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, background: t.n85, color: t.n0, fontFamily: t.fontMono, fontSize: "0.8rem", outline: "none" };
  const label = { fontSize: "0.72rem", fontWeight: weight.semibold, color: t.n2, textTransform: "uppercase" as const, letterSpacing: "0.05em" };
  const toggle = (on: boolean, set: (v: boolean) => void, text: string, count: number) => (
    <label style={{ display: "flex", alignItems: "center", gap: 9, padding: "8px 0", cursor: "pointer", fontSize: "0.83rem", color: t.n1 }}>
      <input type="checkbox" checked={on} onChange={(e) => set(e.target.checked)} style={{ width: 15, height: 15, accentColor: t.primary6 }} />
      {text} <span style={{ color: t.n3, fontFamily: t.fontMono, fontSize: "0.74rem" }}>(now {count})</span>
    </label>
  );
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 56 }}>
      <div ref={dialogRef} {...DIALOG} aria-label="Edit the corrected state" onClick={(e) => e.stopPropagation()} style={{ width: 520, background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, display: "flex", flexDirection: "column", overflow: "hidden", outline: "none" }}>
        <div style={{ padding: "18px 22px 14px", borderBottom: `1px solid ${t.n7}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>✎ Edit the corrected state</div>
            <div style={{ marginTop: 3, fontSize: "0.8rem", color: t.n2 }}>Change the world, then re-verify against the live gym for a real verdict.</div>
          </div>
          <span onClick={onClose} style={{ cursor: "pointer", color: t.n3, display: "inline-flex" }}><Icon name="close" size={18} /></span>
        </div>
        <div style={{ padding: "16px 22px", display: "flex", flexDirection: "column", gap: 14 }}>
          <div>
            <span style={label}>Logged-in user</span>
            <input value={user} onChange={(e) => setUser(e.target.value)} placeholder="(none)" style={field} />
          </div>
          <div>
            <span style={label}>Applied promo</span>
            <input value={promo} onChange={(e) => setPromo(e.target.value)} placeholder="(none)" style={field} />
          </div>
          <div style={{ borderTop: `1px solid ${t.n8}`, paddingTop: 4 }}>
            {toggle(voidOrders, setVoidOrders, "Void all orders", nOrders)}
            {toggle(emptyCart, setEmptyCart, "Empty the cart", nCart)}
            {toggle(voidReturns, setVoidReturns, "Void all returns", nReturns)}
            {toggle(voidSubs, setVoidSubs, "Cancel all subscriptions", nSubs)}
          </div>
        </div>
        <div style={{ padding: "14px 22px", borderTop: `1px solid ${t.n7}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.74rem", color: t.n3, fontFamily: t.fontMono }}>{Object.keys(edits).length} edit{Object.keys(edits).length === 1 ? "" : "s"}</span>
          <Button variant="primary" disabled={busy || Object.keys(edits).length === 0} onClick={async () => { setBusy(true); await onApply(edits); }} style={{ minHeight: 40 }}>
            {busy ? "Re-verifying…" : "Re-verify against gym"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function AutogenPanel({ result, onClose }: { result: AutogenResult; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalA11y(onClose, dialogRef);
  const ok = result.oracle;
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 55 }}>
      <div ref={dialogRef} {...DIALOG} aria-label="Generated verifier suite" onClick={(e) => e.stopPropagation()} style={{ width: 660, maxHeight: "80vh", background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, display: "flex", flexDirection: "column", overflow: "hidden", outline: "none" }}>
        <div style={{ padding: "18px 20px 14px", borderBottom: `1px solid ${t.n7}` }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>🤖 Reward agent — generated verifier suite</span>
            <span onClick={onClose} style={{ cursor: "pointer", color: t.n3, display: "inline-flex" }}><Icon name="close" size={18} /></span>
          </div>
          <div style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ padding: "3px 9px", borderRadius: 6, fontSize: "0.72rem", fontWeight: weight.bold, background: ok ? t.greenLite : t.redLite, color: ok ? t.greenDark : t.redDark }}>
              {ok ? "✓ Oracle-valid (0 on initial · 1 on golden)" : "Not oracle-valid"}
            </span>
            <span style={{ fontSize: "0.75rem", color: t.n2 }}>
              {result.stateChecks} state · {result.policyChecks} policy · {result.iterations} iteration{result.iterations === 1 ? "" : "s"}
              {result.gate ? ` · gate ${result.gate.initialReward}/${result.gate.goldenReward}` : ""}
            </span>
          </div>
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "8px 0" }}>
          {result.suite.map((v) => {
            const isPolicy = (v.check as { kind?: string }).kind === "trace_policy";
            return (
              <div key={v.id} style={{ padding: "9px 20px", borderBottom: `1px solid ${t.n8}`, display: "flex", gap: 10, alignItems: "flex-start" }}>
                <span style={{ marginTop: 1, padding: "2px 7px", borderRadius: 5, fontSize: "0.64rem", fontWeight: weight.bold, textTransform: "uppercase", background: isPolicy ? "color-mix(in srgb, #a855f7 15%, transparent)" : t.surfaceTint, color: isPolicy ? "#7c3aed" : t.n2, whiteSpace: "nowrap" }}>{isPolicy ? "policy" : v.level}</span>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: "0.83rem", color: t.n1 }}>{v.assertion}</div>
                  <div style={{ marginTop: 2, fontFamily: t.fontMono, fontSize: "0.7rem", color: t.n3, wordBreak: "break-word" }}>{JSON.stringify(v.check)}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function QaPanel({ onClose, reviewer }: { onClose: () => void; reviewer: string }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalA11y(onClose, dialogRef);
  const [tasks, setTasks] = useState<QaTaskRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [subs, setSubs] = useState<QaSubmission[] | null>(null);
  const [busy, setBusy] = useState(false);
  const reload = () => fetchQaTasks().then(setTasks);
  useEffect(() => { void reload(); }, []);
  const openTask = async (id: string) => { setSelected(id); setSubs(null); const r = await fetchQaSubmissions(id); setSubs(r?.submissions ?? []); };
  const accept = async (sessionId: string) => {
    if (!selected) return;
    setBusy(true);
    await adjudicate(selected, sessionId, reviewer);
    await openTask(selected);
    await reload();
    setBusy(false);
  };
  const badge = (row: QaTaskRow) => {
    if (row.adjudicated) return { txt: "adjudicated", bg: t.greenLite, fg: t.greenDark };
    if (row.disputed) return { txt: `disputed · ${Math.round((row.agreement ?? 0) * 100)}%`, bg: t.redLite, fg: t.redDark };
    return { txt: "unanimous", bg: t.surfaceTint, fg: t.n2 };
  };
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 55 }}>
      <div ref={dialogRef} {...DIALOG} aria-label="Multi-annotator QA" onClick={(e) => e.stopPropagation()} style={{ width: 840, height: "78vh", background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, display: "flex", flexDirection: "column", overflow: "hidden", outline: "none" }}>
        <div style={{ padding: "18px 22px 14px", borderBottom: `1px solid ${t.n7}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div>
            <div style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>⚖ Multi-annotator QA</div>
            <div style={{ marginTop: 3, fontSize: "0.8rem", color: t.n2 }}>Agreement across annotators; accept one submission as the golden. Reviewing as <span style={{ fontFamily: t.fontMono, fontSize: "0.74rem" }}>{reviewer}</span>.</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <a href="/api/export/dataset.jsonl?accepted=true" download style={{ fontSize: "0.74rem", fontWeight: weight.semibold, color: t.primary6, textDecoration: "none", whiteSpace: "nowrap" }} title="Download the accepted golden samples as JSONL (the deliverable dataset)">⬇ Export golden dataset</a>
            <span onClick={onClose} style={{ cursor: "pointer", color: t.n3, display: "inline-flex" }}><Icon name="close" size={18} /></span>
          </div>
        </div>
        <div style={{ flex: 1, display: "flex", minHeight: 0 }}>
          <div style={{ width: 320, borderRight: `1px solid ${t.n7}`, overflowY: "auto" }}>
            {tasks == null ? (
              <div style={{ padding: 24, color: t.n3, fontSize: "0.85rem" }}>Loading…</div>
            ) : tasks.length === 0 ? (
              <div style={{ padding: 24, color: t.n3, fontSize: "0.85rem" }}>No submissions yet. Submit a task as a couple of annotators (change the identity in the header) to see agreement here.</div>
            ) : tasks.map((row) => {
              const b = badge(row);
              return (
                <div key={row.taskExternalId} onClick={() => openTask(row.taskExternalId)} style={{ padding: "11px 18px", cursor: "pointer", borderBottom: `1px solid ${t.n8}`, background: selected === row.taskExternalId ? t.surfaceTint : "transparent" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <span style={{ fontFamily: t.fontMono, fontSize: "0.76rem", color: t.n1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.taskExternalId}</span>
                    <span style={{ fontSize: "0.64rem", fontWeight: weight.bold, padding: "2px 7px", borderRadius: 5, background: b.bg, color: b.fg, whiteSpace: "nowrap" }}>{b.txt}</span>
                  </div>
                  <div style={{ marginTop: 3, fontSize: "0.72rem", color: t.n3 }}>{row.submissions} submissions · {row.annotators} annotators · majority reward {row.majorityReward}</div>
                </div>
              );
            })}
          </div>
          <div style={{ flex: 1, overflowY: "auto", padding: "8px 0" }}>
            {selected == null ? (
              <div style={{ padding: 28, color: t.n3, fontSize: "0.85rem", textAlign: "center" }}>Select a task to see each annotator's submission.</div>
            ) : subs == null ? (
              <div style={{ padding: 24, color: t.n3, fontSize: "0.85rem" }}>Loading submissions…</div>
            ) : subs.map((s) => (
              <div key={s.sessionId} style={{ padding: "12px 22px", borderBottom: `1px solid ${t.n8}`, display: "flex", alignItems: "center", gap: 12 }}>
                <span style={{ width: 30, height: 30, borderRadius: t.radiusFull, background: t.primary7, color: t.n9, display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: "0.78rem", fontWeight: weight.bold, flexShrink: 0 }}>{s.annotator.charAt(0).toUpperCase()}</span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: "0.82rem", color: t.n1, fontFamily: t.fontMono }}>{s.annotator}</div>
                  <div style={{ fontSize: "0.72rem", color: t.n3, marginTop: 1 }}>{s.kind}{s.override ? " · overridden" : ""} · {new Date(s.at).toLocaleString()}</div>
                </div>
                <span style={{ fontFamily: t.fontMono, fontSize: "0.78rem", fontWeight: weight.bold, padding: "3px 10px", borderRadius: 6, background: s.reward === 1 ? t.greenLite : t.redLite, color: s.reward === 1 ? t.greenDark : t.redDark }}>reward {s.reward}</span>
                {s.accepted ? (
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: "0.72rem", fontWeight: weight.bold, color: t.greenDark }}><Icon name="check" size={14} stroke={2.4} color={t.greenDark} /> accepted</span>
                ) : (
                  <span onClick={busy ? undefined : () => accept(s.sessionId)} style={{ fontSize: "0.72rem", fontWeight: weight.semibold, color: busy ? t.n4 : t.primary6, cursor: busy ? "default" : "pointer", padding: "5px 11px", border: `1px solid ${t.n6}`, borderRadius: t.radiusLg, whiteSpace: "nowrap" }}>Accept as golden</span>
                )}
                <span onClick={() => downloadSampleBundle(s.sessionId)} title="Download this sample's golden bundle (JSON)" style={{ fontSize: "0.72rem", fontWeight: weight.semibold, color: t.n2, cursor: "pointer", whiteSpace: "nowrap" }}>⬇ bundle</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function GymPicker({ onClose, onPick }: { onClose: () => void; onPick: (id: string) => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalA11y(onClose, dialogRef);
  const [all, setAll] = useState<string[] | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    fetchGymStatus().then((st) => {
      setConnected(st.connected);
      if (st.connected) fetchGymTasks().then((ts) => setAll(ts ? ts.map((x) => x.id) : []));
      else setAll([]);
    });
  }, []);
  const list = (all ?? []).filter((id) => id.toLowerCase().includes(q.toLowerCase())).slice(0, 200);
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 }}>
      <div ref={dialogRef} {...DIALOG} aria-label="Load a gym task" onClick={(e) => e.stopPropagation()} style={{ width: 620, maxHeight: "76vh", background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, display: "flex", flexDirection: "column", overflow: "hidden", outline: "none" }}>
        <div style={{ padding: "18px 20px 12px", borderBottom: `1px solid ${t.n7}` }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <span style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>Load a real gym task</span>
            <span onClick={onClose} style={{ cursor: "pointer", color: t.n3, display: "inline-flex" }}><Icon name="close" size={18} /></span>
          </div>
          <div style={{ marginTop: 4, fontSize: "0.8125rem", color: t.n2 }}>Runs the oracle agent live in the gym, then loads the real run + its milestones to review. {connected === false ? "" : all == null ? "Loading catalog…" : `${all.length} tasks.`}</div>
          {connected !== false && (
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter tasks — e.g. buy, refund, subscription…" style={{ marginTop: 12, width: "100%", boxSizing: "border-box", padding: "9px 12px", borderRadius: t.radiusLg, border: `1px solid ${t.n6}`, fontFamily: t.fontPrimary, fontSize: "0.875rem", color: t.n0, outline: "none" }} />
          )}
        </div>
        <div style={{ flex: 1, overflowY: "auto", padding: "8px 0" }}>
          {connected === false ? (
            <div style={{ padding: "28px 24px", textAlign: "center" }}>
              <div style={{ fontSize: "0.9rem", fontWeight: weight.bold, color: t.n0 }}>Live gym not connected</div>
              <div style={{ margin: "8px auto 0", maxWidth: 440, fontSize: "0.82rem", color: t.n2, lineHeight: 1.55 }}>
                The 312 live gym tasks need a running gym server (set <span style={{ fontFamily: t.fontMono, fontSize: "0.76rem" }}>GYM_URL</span> for a hosted deploy). Everything else — the sample tasks, the correction &amp; re-run flow, the 5-level verifier suite, scoring, and persistence — works without it.
              </div>
              <span onClick={onClose} style={{ display: "inline-block", marginTop: 16, padding: "8px 16px", borderRadius: t.radiusLg, background: t.primary6, color: t.n9, fontSize: "0.82rem", fontWeight: weight.semibold, cursor: "pointer" }}>Back to the sample tasks</span>
            </div>
          ) : all == null ? (
            <div style={{ padding: 24, textAlign: "center", color: t.n3, fontSize: "0.85rem" }}>Fetching the gym catalog…</div>
          ) : list.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: t.n3, fontSize: "0.85rem" }}>{all.length === 0 ? "No gym tasks available." : "No tasks match."}</div>
          ) : (
            list.map((id) => (
              <div key={id} onClick={() => onPick(id)} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 20px", cursor: "pointer", fontSize: "0.84rem", color: t.n1, borderBottom: `1px solid ${t.n8}` }}
                onMouseEnter={(e) => (e.currentTarget.style.background = t.surfaceTint)} onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                <span style={{ fontFamily: t.fontMono, fontSize: "0.8rem" }}>{id}</span>
                <Icon name="chevronRight" size={15} color={t.n4} />
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function GymLoading({ taskId, phase }: { taskId: string; phase: "queued" | "running" | "done" | "error" }) {
  const heading = phase === "queued" ? "Queued — waiting for the gym…" : "Running the agent in the gym…";
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(13,13,13,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 60 }}>
      <div style={{ width: 420, background: t.n9, borderRadius: t.radius2xl, boxShadow: t.shadowXl, padding: 26, textAlign: "center" }}>
        <div style={{ fontSize: "1rem", fontWeight: weight.bold, color: t.n0 }}>{heading}</div>
        <div style={{ marginTop: 8, fontSize: "0.84rem", color: t.n2, lineHeight: 1.5 }}>Driving a real browser through <span style={{ fontFamily: t.fontMono, fontSize: "0.78rem" }}>{taskId}</span> and scoring it with the real milestone verifiers. This takes a few seconds.</div>
        <div style={{ marginTop: 16, height: 4, background: t.n7, borderRadius: 3, overflow: "hidden" }}>
          <div style={{ height: "100%", width: "40%", background: t.primary6, borderRadius: 3, animation: "gymbar 1.1s ease-in-out infinite" }} />
        </div>
        <style>{"@keyframes gymbar{0%{margin-left:-40%}100%{margin-left:100%}}"}</style>
      </div>
    </div>
  );
}

export function TaskReview({ initialTaskId, onExitToTasks }: { initialTaskId?: string; onExitToTasks?: () => void } = {}) {
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [index, setIndex] = useState(0);
  const [data, setData] = useState<ReviewData | null>(null);
  const [gymData, setGymData] = useState<ReviewData | null>(null);
  const [gymLoading, setGymLoading] = useState<string | null>(null);
  const [gymPhase, setGymPhase] = useState<"queued" | "running" | "done" | "error">("queued");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [gymError, setGymError] = useState<string | null>(null);
  const [freshNonce, setFreshNonce] = useState(0); // >0 forces a new session on remount ("New annotation")
  const [qaOpen, setQaOpen] = useState(false);
  const [queueSet, setQueueSet] = useState<"breakers" | "fixtures">("breakers");
  const [gymAdhoc, setGymAdhoc] = useState(false); // true = loaded off-queue via the Gym picker (not the main queue)
  const { annotator } = useAuth(); // the signed-in identity — replaces the old free-text "AS" field
  const [profileOpen, setProfileOpen] = useState(false);

  // The review queue: the 85 breakers by default, or the demo fixtures. When the
  // annotator arrived by picking a task on the My-tasks board, start the pager on
  // THAT task rather than the first — the pager still works, they just open where
  // they clicked.
  useEffect(() => {
    let alive = true;
    fetchTasks(queueSet).then((ts) => {
      if (!alive) return;
      setTasks(ts);
      const at = initialTaskId ? ts.findIndex((x) => x.id === initialTaskId) : -1;
      setIndex(at >= 0 ? at : 0);
    });
    return () => { alive = false; };
  }, [queueSet, initialTaskId]);

  const loadGym = async (id: string, adhoc = false) => {
    setPickerOpen(false);
    setGymError(null);
    setGymAdhoc(adhoc);
    setGymLoading(id);
    // The annotator DOES the task; nothing is run on their behalf. This used to
    // drive a live model on every task select and make them wait for it, which is
    // why a task dead-ended entirely whenever no gym was reachable.
    const manual = await getManualReview(id);
    setGymLoading(null);
    // Deliberately NO fallback to a persisted agent run. That fallback is what
    // put a pre-recorded agent trajectory on screen for any task that had ever
    // been reviewed on the old flow — steps this annotator did not take, which
    // would then sit in the same list as the ones they did. A task that cannot
    // be loaded has to say so rather than quietly show somebody else's work.
    if (manual) setGymData(manual);
    else setGymError(id);
  };

  const currentTask = tasks[index];
  const taskId = currentTask?.id ?? TASK_ID;
  // A new task (or entering/exiting the gym) resets the fresh-start intent.
  useEffect(() => { setFreshNonce(0); }, [taskId, gymData?.task.id]);
  // Load the selected task. Breakers (source "gym") run the agent LIVE in the gym
  // and load the real trajectory; demo fixtures load a baked review payload.
  useEffect(() => {
    let alive = true;
    setData(null);
    setGymData(null);
    if (!tasks.length) return;
    if (currentTask?.source === "gym") {
      void loadGym(taskId, false); // a QUEUE breaker — keep the Task N/M pager
    } else {
      fetchReview(taskId).then((r) => {
        if (!alive) return;
        setData(r.data);
        // eslint-disable-next-line no-console
        console.info(`[annotator] review ${taskId} loaded from ${r.source}`);
      });
    }
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, tasks.length]);

  const total = tasks.length || 1;
  const effective = gymData ?? data;
  const nav: TaskNav = {
    index: Math.min(index, total - 1),
    total,
    onPrev: () => { setGymData(null); setIndex((i) => Math.max(0, i - 1)); },
    onNext: () => { setGymData(null); setIndex((i) => Math.min(total - 1, i + 1)); },
    onSkip: () => { setGymData(null); setIndex((i) => (i + 1) % total); },
    onBrowseGym: () => setPickerOpen(true),
    gymTaskId: gymData?.task.id ?? null,
    gymAdhoc,
    // Exiting an off-queue pick returns to the current queue task (reloading it
    // if it's a breaker); it does not leave the queue.
    onExitGym: () => {
      setGymAdhoc(false);
      setGymData(null);
      if (currentTask?.source === "gym") void loadGym(taskId, false);
    },
    onOpenQa: () => setQaOpen(true),
    annotator,
    onOpenProfile: () => setProfileOpen(true),
    onBackToTasks: onExitToTasks,
    queueSet,
    onToggleQueue: () => { setGymData(null); setQueueSet((q) => (q === "breakers" ? "fixtures" : "breakers")); },
    // Prompt edit → re-drive the WHOLE run from the initial state under the new
    // brief (a live gpt-5.5 run), then remount a FRESH review of that new run.
    // Re-drives the DISPLAYED task (an off-queue picker task, else the queue task)
    // — defined whenever a gym task is on screen, not only when the queue task is gym.
    onPromptRerun: (gymData || currentTask?.source === "gym") ? async (prompt: string) => {
      const rid = gymData?.task.id ?? taskId; // the task actually shown, not always the queue task
      setGymError(null);
      setGymPhase("queued");
      setGymLoading(rid);
      const rv = await runGymReview(rid, "openai", 0, { onStatus: setGymPhase, brief: prompt });
      setGymLoading(null);
      if (rv) { setGymData(rv); setFreshNonce((n) => n + 1); } // new trajectory + fresh session
      else setGymError(rid);
    } : undefined,
  };

  return (
    <>
      {effective ? (
        <ReviewScreen
          key={`${gymData ? `gym:${gymData.task.id}` : taskId}#${freshNonce}#${annotator?.email ?? ""}`}
          data={effective}
          nav={nav}
          startFresh={freshNonce > 0}
          onStartNew={() => setFreshNonce((n) => n + 1)}
        />
      ) : (
        <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", color: t.n3, fontFamily: t.fontPrimary }}>Loading task…</div>
      )}
      {pickerOpen && <GymPicker onClose={() => setPickerOpen(false)} onPick={(id) => loadGym(id, true)} />}
      {qaOpen && <QaPanel onClose={() => setQaOpen(false)} reviewer={annotator?.email ?? ""} />}
      {profileOpen && <ProfilePanel onClose={() => setProfileOpen(false)} />}
      {gymLoading && <GymLoading taskId={gymLoading} phase={gymPhase} />}
      {gymError && (
        <div style={{ position: "fixed", left: "50%", bottom: 24, transform: "translateX(-50%)", display: "flex", alignItems: "center", gap: 16, background: t.redLite, color: t.redDark, border: `1px solid color-mix(in srgb, ${t.red} 42%, ${t.n9})`, padding: "10px 16px", borderRadius: t.radiusLg, fontSize: "0.84rem", fontWeight: weight.semibold, zIndex: 70, fontFamily: t.fontPrimary, boxShadow: t.shadowLg }}>
          <span>Couldn't run <span style={{ fontFamily: t.fontMono }}>{gymError}</span> — the model produced no run (it may be rate-limited, or the gym is down).</span>
          <span onClick={() => { const id = gymError; setGymError(null); if (id) void loadGym(id, gymAdhoc); }} style={{ cursor: "pointer", textDecoration: "underline", whiteSpace: "nowrap" }}>Retry</span>
          <span onClick={() => setGymError(null)} style={{ cursor: "pointer", opacity: 0.7 }}>✕</span>
        </div>
      )}
    </>
  );
}
