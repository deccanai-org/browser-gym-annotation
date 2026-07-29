/**
 * Client for the version-graph endpoints (backend/app/api/versions.py).
 *
 * Deliberately NOT built on api.ts's `post`/`send`: those collapse 4xx, 5xx and
 * a dead network into `null`. Every mutation here is a compare-and-swap that can
 * legitimately LOSE, and "someone else moved this attempt" (409) has to reach
 * the annotator as its own outcome. A caller that cannot tell 409 from offline
 * either retries over a decision somebody just made, or drops it in silence.
 */

export type VersionStatus = "candidate" | "approved" | "rejected" | "published";
export type StepVerdict = "pending" | "verified" | "rejected";
export type ForkMode = "before" | "after";

export interface VersionNode {
  id: string;
  versionNo: number;
  parentId: string | null;
  kind: string; // agent_run | agent_correction | human_manual
  status: VersionStatus;
  revision: number;
  producer: string;
  forkBeforeStepId: string | null;
  forkCheckpointId: string | null;
  isHead: boolean;
  stepCount: number;
  createdAt: string;
}

export interface VersionGraphData {
  attemptId: string;
  revision: number;
  headVersionId: string | null;
  agentCallCount: number;
  versions: VersionNode[];
  verdicts: Record<string, { verdict: StepVerdict; note: string }>;
}

export interface VersionStep {
  displayIdx: number;
  stepId: string;
  versionId: string | null;
  /** True when this row is the parent's SAME step row, shared — not a copy. */
  inherited: boolean;
  actor: string;
  type: string;
  description: string;
  url: string;
  image: string;
  reasoning: string;
  humanIntent: string;
  guidance: string;
  verdict: StepVerdict;
}

export interface VersionStepsData {
  versionId: string;
  versionNo: number;
  steps: VersionStep[];
}

export interface AgentRunJob {
  id: string;
  status: "queued" | "running" | "done" | "error";
  sourceVersionId: string | null;
  resultVersionId: string | null;
  countsAgainstCap: boolean;
  error: string;
  createdAt: string;
}

export interface RunsData {
  agentCallCount: number;
  cap: number | null;
  runs: AgentRunJob[];
}

export interface AgentRunStarted {
  jobId: string;
  runId?: string;
  /** The CANDIDATE this run will fill in. It is not the head and never becomes
   *  one on its own — the annotator selects it. */
  versionId: string | null;
  status?: string;
  replayed?: boolean;
}

// --------------------------------------------------------------------------- transport
export type FailureKind =
  | "conflict" // 409 — a lost CAS, or a fork point that is not in the parent's chain
  | "capped" // 429 — the attempt is out of agent runs
  | "invalid" // 422
  | "denied" // 401/403
  | "missing" // 404
  | "offline" // never reached the server
  | "error";

export interface ApiFailure {
  ok: false;
  kind: FailureKind;
  status: number;
  message: string;
}

export type ApiResult<T> = { ok: true; value: T } | ApiFailure;

const KIND: Record<number, FailureKind> = {
  401: "denied",
  403: "denied",
  404: "missing",
  409: "conflict",
  422: "invalid",
  429: "capped",
};

/** FastAPI `detail` is a string on most of these routes but an object on the
 *  replay rejections — render both rather than showing "[object Object]". */
function detailText(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; reason?: string; at?: number };
    const parts = [d.error, d.reason].filter(Boolean);
    if (parts.length) return d.at != null ? `${parts.join(" — ")} (at step ${d.at})` : parts.join(" — ");
  }
  return `Request failed (${status}).`;
}

async function request<T>(url: string, init?: RequestInit): Promise<ApiResult<T>> {
  let res: Response;
  try {
    res = await fetch(url, { credentials: "include", ...init });
  } catch {
    return { ok: false, kind: "offline", status: 0, message: "Cannot reach the server." };
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    return { ok: false, kind: KIND[res.status] ?? "error", status: res.status, message: detailText(body, res.status) };
  }
  try {
    return { ok: true, value: (await res.json()) as T };
  } catch {
    return { ok: false, kind: "error", status: res.status, message: "The server sent a response we could not read." };
  }
}

function post<T>(url: string, body: unknown): Promise<ApiResult<T>> {
  return request<T>(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

const at = (sessionId: string) => `/api/sessions/${encodeURIComponent(sessionId)}`;

// --------------------------------------------------------------------------- reads
export function fetchVersionGraph(sessionId: string): Promise<ApiResult<VersionGraphData>> {
  return request<VersionGraphData>(`${at(sessionId)}/versions`);
}

export function fetchVersionSteps(sessionId: string, versionId: string): Promise<ApiResult<VersionStepsData>> {
  return request<VersionStepsData>(`${at(sessionId)}/versions/${encodeURIComponent(versionId)}/steps`);
}

export function fetchRuns(sessionId: string): Promise<ApiResult<RunsData>> {
  return request<RunsData>(`${at(sessionId)}/runs`);
}

/** Materialize v1 from the canonical recorded run. Idempotent, so the UI calls
 *  it on open rather than guessing whether a lineage exists yet. */
export function ensureBaseline(sessionId: string): Promise<ApiResult<VersionNode>> {
  return post<VersionNode>(`${at(sessionId)}/versions/baseline`, {});
}

// --------------------------------------------------------------------------- forking
export interface ForkPoint {
  parentVersionId: string;
  stepId: string;
  mode: ForkMode;
}

/**
 * Reject this step. It will NOT appear in the child — the branch starts from the
 * state that PRECEDED it.
 *
 * Two named constructors instead of one `mode` argument, because a bare
 * `mode`/boolean at the call site is precisely how "throw this action away" and
 * "keep this action" get transposed, and the resulting golden trajectory still
 * looks plausible.
 */
export function rejecting(parentVersionId: string, stepId: string): ForkPoint {
  return { parentVersionId, stepId, mode: "before" };
}

/** Keep this step and branch from the state it produced. */
export function continuingAfter(parentVersionId: string, stepId: string): ForkPoint {
  return { parentVersionId, stepId, mode: "after" };
}

/**
 * The words for each fork mode, in one place. Button label, confirmation line
 * and the badge on the resulting version all read from here so they cannot
 * drift apart and describe two different operations.
 */
export const FORK_COPY = {
  before: {
    action: "Reject this step",
    hint: "Branches from the state before it. The rejected step will not appear in the new version.",
    outcome: "rejected",
  },
  after: {
    action: "Continue after this step",
    hint: "Keeps this step and branches from the state it produced.",
    outcome: "kept",
  },
} as const;

export function createFork(sessionId: string, point: ForkPoint): Promise<ApiResult<VersionNode>> {
  return post<VersionNode>(`${at(sessionId)}/versions/fork`, point);
}

/**
 * Hand a branch to a batch agent. Returns as soon as the CANDIDATE exists — the
 * result is never adopted here. Selecting it is a separate, human command
 * (`selectHeadOrReload`), which is what stops a run that finishes late from
 * resurrecting a branch the annotator already moved past.
 */
export function startAgentRun(
  sessionId: string,
  point: ForkPoint,
  opts?: { correction?: string; agent?: string; idempotencyKey?: string },
): Promise<ApiResult<AgentRunStarted>> {
  return post<AgentRunStarted>(`${at(sessionId)}/versions/agent-run`, {
    ...point,
    correction: opts?.correction ?? "",
    agent: opts?.agent ?? "llm",
    idempotencyKey: opts?.idempotencyKey ?? "",
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Poll a branch run to completion by its RESULT VERSION rather than the
 * background job id: the durable `AgentRunJob` row is keyed that way in both the
 * fresh-enqueue and the idempotent-replay response, and it outlives the
 * in-memory job store.
 */
export async function waitForAgentRun(
  sessionId: string,
  versionId: string,
  opts?: { intervalMs?: number; timeoutMs?: number; onStatus?: (s: AgentRunJob["status"]) => void },
): Promise<AgentRunJob | null> {
  const interval = opts?.intervalMs ?? 2000;
  const deadline = Date.now() + (opts?.timeoutMs ?? 300_000);
  let last: AgentRunJob["status"] | null = null;
  while (Date.now() < deadline) {
    await sleep(interval);
    const res = await fetchRuns(sessionId);
    if (!res.ok) continue; // a transient blip is not a failed run — keep polling
    const job = res.value.runs.find((r) => r.resultVersionId === versionId);
    if (!job) continue;
    if (job.status !== last) {
      last = job.status;
      opts?.onStatus?.(job.status);
    }
    if (job.status === "done" || job.status === "error") return job;
  }
  return null; // client-side timeout guard; the run keeps going server-side
}

// --------------------------------------------------------------------------- compare-and-swap
export interface SelectResult {
  headVersionId: string;
  revision: number;
}

export interface StatusResult {
  versionId: string;
  status: VersionStatus;
  revision: number;
}

export interface CasOutcome<T> {
  ok: boolean;
  value: T | null;
  /** What to tell the annotator. Null only on success. */
  notice: string | null;
  /** The lineage as re-read after a lost CAS. Render THIS, not what you had. */
  reloaded: VersionGraphData | null;
}

export const MOVED_ON =
  "This attempt moved on while you were looking at it. The lineage has been reloaded — check which version is head, then decide again.";

export const DECIDED_ELSEWHERE =
  "Someone else already decided this version. The lineage has been reloaded — review the current status before deciding again.";

async function cas<T>(attempt: Promise<ApiResult<T>>, sessionId: string, notice: string): Promise<CasOutcome<T>> {
  const res = await attempt;
  if (res.ok) return { ok: true, value: res.value, notice: null, reloaded: null };
  if (res.kind !== "conflict") return { ok: false, value: null, notice: res.message, reloaded: null };
  // A lost CAS is somebody's decision, not a transient error. Re-read and hand
  // the caller the current truth. Re-issuing the write with the server's fresh
  // revision would overwrite exactly the decision that just beat us — which is
  // the failure the whole revision scheme exists to prevent.
  const fresh = await fetchVersionGraph(sessionId);
  return { ok: false, value: null, notice, reloaded: fresh.ok ? fresh.value : null };
}

/** Advance the attempt head. Never called on the client's behalf by anything
 *  else — head only moves when the annotator says so. */
export function selectHeadOrReload(
  sessionId: string,
  versionId: string,
  expectedRevision: number,
): Promise<CasOutcome<SelectResult>> {
  return cas(post<SelectResult>(`${at(sessionId)}/versions/select`, { versionId, expectedRevision }), sessionId, MOVED_ON);
}

/** QC decision on a version. Content never changes; only status moves. */
export function setStatusOrReload(
  sessionId: string,
  versionId: string,
  status: VersionStatus,
  expectedRevision: number,
): Promise<CasOutcome<StatusResult>> {
  return cas(
    post<StatusResult>(`${at(sessionId)}/versions/${encodeURIComponent(versionId)}/status`, { status, expectedRevision }),
    sessionId,
    DECIDED_ELSEWHERE,
  );
}

// --------------------------------------------------------------------------- verdicts
export interface VerdictResult {
  stepId: string;
  verdict: StepVerdict;
  note: string;
}

/** Keyed by the step's stable id, not its position — which is why the verdict
 *  survives a re-fork and shows up on the same step in every branch. */
export function setStepVerdict(
  sessionId: string,
  stepId: string,
  verdict: StepVerdict,
  note = "",
): Promise<ApiResult<VerdictResult>> {
  return post<VerdictResult>(`${at(sessionId)}/steps/verdict`, { stepId, verdict, note });
}

/** Apply a just-written verdict locally so the row updates without a refetch.
 *  Matches on stepId, so an inherited row updates in whichever version it is
 *  currently being viewed from. */
export function applyVerdict(steps: VersionStep[], stepId: string, verdict: StepVerdict): VersionStep[] {
  return steps.map((s) => (s.stepId === stepId ? { ...s, verdict } : s));
}

/** Re-project the attempt-wide verdict map onto a version's flattened steps.
 *  Used after switching versions on a stale steps payload. */
export function withVerdicts(steps: VersionStep[], verdicts: VersionGraphData["verdicts"]): VersionStep[] {
  return steps.map((s) => ({ ...s, verdict: verdicts[s.stepId]?.verdict ?? "pending" }));
}

export function verdictTally(steps: VersionStep[]): { verified: number; rejected: number; pending: number } {
  const tally = { verified: 0, rejected: 0, pending: 0 };
  for (const s of steps) tally[s.verdict] += 1;
  return tally;
}

// --------------------------------------------------------------------------- lineage shape
export interface LineageRow {
  version: VersionNode;
  depth: number;
  /** True when no later sibling follows — the rail draws an elbow, not a tee. */
  isLast: boolean;
}

/**
 * Depth-first order for the rail: parents above their children, siblings by
 * version number.
 *
 * A node whose parent is missing from the payload is treated as a root instead
 * of being skipped. Dropping it would hide a branch that exists on the server,
 * and an annotator cannot reason about a lineage that renders fewer versions
 * than the attempt actually has.
 */
export function buildLineage(versions: VersionNode[]): LineageRow[] {
  const byId = new Map(versions.map((v) => [v.id, v]));
  const kids = new Map<string | null, VersionNode[]>();
  for (const v of [...versions].sort((a, b) => a.versionNo - b.versionNo)) {
    const parent = v.parentId && byId.has(v.parentId) ? v.parentId : null;
    kids.set(parent, [...(kids.get(parent) ?? []), v]);
  }
  const out: LineageRow[] = [];
  const walk = (parent: string | null, depth: number) => {
    const row = kids.get(parent) ?? [];
    row.forEach((v, i) => {
      out.push({ version: v, depth, isLast: i === row.length - 1 });
      walk(v.id, depth + 1);
    });
  };
  walk(null, 0);
  return out;
}

export function headOf(graph: VersionGraphData | null): VersionNode | null {
  if (!graph) return null;
  return graph.versions.find((v) => v.id === graph.headVersionId) ?? null;
}

export function runsLeft(runs: RunsData | null): number | null {
  if (!runs || runs.cap == null) return null;
  return Math.max(0, runs.cap - runs.agentCallCount);
}

export const KIND_LABEL: Record<string, string> = {
  agent_run: "canonical run",
  agent_correction: "correction",
  human_manual: "human edit",
};
