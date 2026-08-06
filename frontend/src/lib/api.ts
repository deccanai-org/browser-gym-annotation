import { APP_COLOR } from "./appColors";
import { reviewFixture } from "../fixtures/reviewPayload";
import type { GymResume, ReviewData, ReviewPayload, Step, TaskListItem } from "./types";

/** The task queue. Default = the 85 breakers; `set=fixtures` = the demo
 *  fixtures. Falls back to a single synthetic row offline. */
export async function fetchTasks(set: "breakers" | "fixtures" | "all" = "breakers"): Promise<TaskListItem[]> {
  try {
    const res = await fetch(`/api/tasks?set=${set}`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = (await res.json()) as TaskListItem[];
    return list.length ? list : fallbackTasks();
  } catch {
    return fallbackTasks();
  }
}

function fallbackTasks(): TaskListItem[] {
  const t = reviewFixture.task;
  return [{ id: t.id, title: t.title, priority: t.priority, meta: t.meta, index: 0, total: 1, source: "fixture" }];
}

// --- My tasks: the board the annotator lands on after signing in ------------

export type MyTaskStatus = "todo" | "in_progress" | "returned" | "in_review" | "submitted";

export interface MyTaskSite {
  app: string;
  title: string;
  domain: string;
}

export interface MyTaskRow {
  id: string;
  title: string;
  category: string;
  difficulty: string;
  prompt: string;
  primaryApp: string;
  sites: MyTaskSite[];
  status: MyTaskStatus;
  resumeStep: number | null;
  sessionId: string | null;
  updatedAt: string | null;
}

export interface MyTasksBoard {
  annotator: { name: string; email: string; role: string };
  batch: string;
  assigned: number;
  quota: { submitted: number; target: number };
  counts: Record<MyTaskStatus | "all", number>;
  nextUp: MyTaskRow | null;
  tasks: MyTaskRow[];
}

/** The signed-in annotator's board: their breakers, each tagged with where THEY
 *  left off. Returns null on failure so the screen can say so rather than show a
 *  fabricated empty board. */
export async function fetchMyTasks(): Promise<MyTasksBoard | null> {
  try {
    const res = await fetch(`/api/my-tasks`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as MyTasksBoard;
  } catch {
    return null;
  }
}

export type SessionStatus =
  | "draft"
  | "steps_approved"
  | "verifiers_generated"
  | "benchmark_run"
  | "submitted";

export interface PersistedVerifier {
  id: string;
  level: string;
  assertion: string;
  code: string;
  check?: Record<string, unknown> | null;
  failsUntilCorrected?: boolean;
  placeholder?: boolean;
  addedByHuman?: boolean;
}

export interface SessionSnapshot {
  sessionId: string;
  taskExternalId: string;
  status: SessionStatus;
  rerunFrom: number | null;
  reviewedThrough: number;
  suite: { suiteId: string; version: number; verifiers: PersistedVerifier[] } | null;
  lastBenchmark: { reward: number; results: Record<string, unknown>; overridden?: string[]; at: string } | null;
  // The persisted correction branch, so the fork restores exactly on reload.
  branch: { fromStep: number; mode: string; steps: Step[] } | null;
  submission: { reward: number; kind: string; override: boolean; at: string } | null;
}

/** A persisted-verifier payload for the suite-save endpoint. */
export interface VerifierPayload {
  id: string;
  level: string;
  assertion: string;
  code: string;
  check?: unknown; // executable IR — persisted so the server recomputes reward authoritatively
  failsUntilCorrected: boolean;
  placeholder: boolean;
  addedByHuman: boolean;
  gymResult?: string; // real gym milestone verdict (pass|fail) — carried onto the exported sample
}

/** Resolve app keys → colors so the render layer stays token-driven. */
function mapPayload(p: ReviewPayload): ReviewData {
  return {
    task: {
      ...p.task,
      allowedSites: p.task.allowedSites.map((s) => ({ host: s.host, color: APP_COLOR[s.app] ?? APP_COLOR.shop })),
    },
    tabs: p.tabs.map((tb) => ({ id: tb.id, title: tb.title, host: tb.host, color: APP_COLOR[tb.app] ?? APP_COLOR.shop })),
    steps: p.steps,
    correctionSeed: p.correctionSeed,
    correctedTail: p.correctedTail,
    verifiers: p.verifiers,
    source: p.source ?? "fixture",
    gymReward: p.gymReward,
    gymResume: p.gymResume,
  };
}

export interface LoadResult {
  data: ReviewData;
  source: "api" | "fallback";
}

/** Fetch a task's review payload from the backend; fall back to the bundled
 *  fixture if the API is unreachable (so the app runs standalone). */
export async function fetchReview(taskId: string): Promise<LoadResult> {
  try {
    const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/review`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = (await res.json()) as ReviewPayload;
    return { data: mapPayload(payload), source: "api" };
  } catch {
    return { data: mapPayload(reviewFixture), source: "fallback" };
  }
}

// ---- session persistence (M4) ---------------------------------------------
// Every call is best-effort: if the backend is down the app still runs from
// memory (offline fixture mode), it just won't persist.

async function post<T>(url: string, body: unknown): Promise<T | null> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include", // send the auth session cookie
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/** POST that REFUSES to fail quietly.
 *
 *  `post` above returns null on any error, which is right for best-effort
 *  persistence but wrong for anything the annotator is waiting on: a 409 came
 *  back as null, the caller treated it as "no data" and carried on, and the
 *  screen showed success for a call that never happened. Anything that gates the
 *  annotator's next move goes through here and gets the server's own sentence.
 */
export async function postStrict<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await refusalText(res));
  return (await res.json()) as T;
}

/** The server's own explanation, or a usable fallback. FastAPI puts it in
 *  `detail`, which is sometimes a string and sometimes a structured object. */
export async function refusalText(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === "string" && d) return d;
    if (d && typeof d === "object") {
      const o = d as { error?: string; reason?: string; at?: number };
      const at = typeof o.at === "number" ? ` (step ${o.at + 1})` : "";
      if (o.error || o.reason) return `${o.error ?? ""}${o.reason ? `: ${o.reason}` : ""}${at}`.trim();
    }
  } catch {
    /* fall through to the status line */
  }
  return `The server refused that (HTTP ${res.status}).`;
}

export interface ShipBlocker {
  code: string;
  message: string;
  at?: number;
}

export interface PrepareShipResult {
  versionId: string | null;
  versionNo: number | null;
  suiteId: string | null;
  suiteCreated: boolean;
  verifiers: { id: string; level: string; assertion: string; gymResult?: string; addedByHuman?: boolean }[];
  blockers: ShipBlocker[];
  canShip: boolean;
}

/** What this attempt still needs before it can ship. Idempotent — it folds any
 *  pending interactions and gives the attempt a verifier suite if it has none,
 *  so calling it on render is both safe and the point. */
export async function prepareShip(sessionId: string): Promise<PrepareShipResult> {
  return postStrict<PrepareShipResult>(`/api/sessions/${sessionId}/prepare-ship`, {});
}

async function send(url: string, method: "PATCH" | "PUT", body: unknown): Promise<void> {
  try {
    await fetch(url, {
      method,
      headers: { "content-type": "application/json" },
      credentials: "include", // send the auth session cookie
      body: JSON.stringify(body),
    });
  } catch {
    /* offline — ignore */
  }
}

/** Resume (or create) this annotator's session for a task. `fresh` forces a new
 *  session; `annotatorEmail` scopes it to a specific annotator (multi-annotator QA). */
export function openSession(taskId: string, opts?: { fresh?: boolean; annotatorEmail?: string }): Promise<SessionSnapshot | null> {
  return post<SessionSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}/sessions`, {
    fresh: opts?.fresh ?? false,
    annotatorEmail: opts?.annotatorEmail,
  });
}

// ---- multi-annotator QA ----------------------------------------------------

export interface QaTaskRow {
  taskExternalId: string; title: string; submissions: number; annotators: number;
  adjudicated: boolean; agreement: number | null; majorityReward: number | null;
  unanimous: boolean; disputed: boolean; distribution: Record<string, number>;
}
export interface QaSubmission {
  sessionId: string; annotator: string; reward: number; kind: string;
  override: boolean; overrideReason: string | null; accepted: boolean; at: string;
}

export async function fetchQaTasks(): Promise<QaTaskRow[]> {
  try {
    const res = await fetch("/api/qa/tasks", { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return ((await res.json()) as { tasks: QaTaskRow[] }).tasks;
  } catch {
    return [];
  }
}

export async function fetchQaSubmissions(taskId: string): Promise<{ title: string; agreement: QaTaskRow; submissions: QaSubmission[] } | null> {
  try {
    const res = await fetch(`/api/qa/tasks/${encodeURIComponent(taskId)}/submissions`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch {
    return null;
  }
}

/** Accept one annotator's submission as the golden for a task. The reviewer is
 *  the authenticated caller — the server reads it from the session cookie, so
 *  there is deliberately no `reviewer` argument to pass, mistype, or forge. */
export async function adjudicate(taskId: string, sessionId: string, note = ""): Promise<boolean> {
  const out = await post<{ accepted: string }>(`/api/qa/tasks/${encodeURIComponent(taskId)}/adjudicate`, { sessionId, note });
  return !!out;
}

// ---- sample packaging / export --------------------------------------------

/** Download one annotation as the deliverable golden-sample bundle (JSON). */
export async function downloadSampleBundle(sessionId: string): Promise<void> {
  try {
    const res = await fetch(`/api/export/samples/${sessionId}`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = new Blob([JSON.stringify(await res.json(), null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `golden_sample_${sessionId.slice(0, 8)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  } catch {
    /* ignore — the button just no-ops offline */
  }
}

export function patchSession(
  sid: string,
  patch: { status?: SessionStatus; rerunFrom?: number; reviewedThrough?: number },
): Promise<void> {
  return send(`/api/sessions/${sid}`, "PATCH", patch);
}

export function saveSuite(sid: string, verifiers: VerifierPayload[]): Promise<void> {
  return send(`/api/sessions/${sid}/suite`, "PUT", { verifiers });
}

export interface RunResult {
  results: Record<string, string>;
  reward: number;
  executed: number;
  overridden: number;
}

/** Execute the verifier suite server-side against the real DOM + state + trace. */
export function runVerifiers(
  sid: string,
  body: { corrected: boolean; verifiers: unknown[]; overrides: string[] },
): Promise<RunResult | null> {
  return post<RunResult>(`/api/sessions/${sid}/run`, body);
}

/** Re-run from a corrected step — persists an immutable branch, returns its steps. */
export function rerunTrajectory(
  sid: string,
  body: { fromStep: number; correction: string; mode?: string },
): Promise<{ fromStep: number; mode: string; steps: Step[] } | null> {
  return post<{ fromStep: number; mode: string; steps: Step[] }>(`/api/sessions/${sid}/rerun`, body);
}

export function submitSession(
  sid: string,
  body: { reward: number; override: boolean; overrideReason?: string; kind?: string },
): Promise<SessionSnapshot | null> {
  return post<SessionSnapshot>(`/api/sessions/${sid}/submit`, body);
}

// ---- real gym tasks (M8) ---------------------------------------------------

export interface GymTaskItem { id: string; category?: string; difficulty?: string }

export interface GymStatus { connected: boolean; url: string }

/** Whether a live gym is reachable. In a hosted deploy with no GYM_URL this is
 *  false, and the UI gates the 312-task features while the fixture flow works. */
export async function fetchGymStatus(): Promise<GymStatus> {
  try {
    const res = await fetch("/api/gym/status", { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as GymStatus;
  } catch {
    return { connected: false, url: "" };
  }
}

/** The catalog of real gym tasks (312), or null if the gym is unreachable. */
export async function fetchGymTasks(): Promise<GymTaskItem[] | null> {
  try {
    const res = await fetch("/api/gym/tasks", { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = (await res.json()) as { tasks: string[] };
    return body.tasks.map((id) => ({ id }));
  } catch {
    return null;
  }
}

export interface GymJob {
  jobId: string;
  status: "queued" | "running" | "done" | "error";
  review?: ReviewPayload;
  error?: string;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export interface ResumeResult { score: number; success: boolean; reward: number }

export interface AutogenResult {
  oracle: boolean;
  stateChecks: number;
  policyChecks: number;
  iterations: number;
  brief: string;
  suite: { id: string; level: string; assertion: string; check: Record<string, unknown> }[];
  gate?: { initialReward: number; goldenReward: number };
}

/** Run the autonomous reward-agent oracle loop for a gym task (auto-generate +
 *  oracle-validate a verifier suite). Async job; polls to the result. */
export async function autogenVerifiers(
  taskId: string,
  seed = 0,
  opts?: { onStatus?: (s: GymJob["status"]) => void },
): Promise<AutogenResult | null> {
  const out = await post<{ jobId: string }>("/api/gym/autogen-verifiers", { taskId, seed, iterations: 5 });
  const jobId = out?.jobId;
  if (!jobId) return null;
  const deadline = Date.now() + 320_000;
  let last: GymJob["status"] | null = null;
  while (Date.now() < deadline) {
    await sleep(2000);
    const j = await pollGymJob(jobId);
    if (!j) continue;
    if (j.status !== last) { last = j.status; opts?.onStatus?.(j.status); }
    if (j.status === "done") return (j.review as unknown as AutogenResult) ?? null;
    if (j.status === "error") return null;
  }
  return null;
}

/** Drive-forward resume (async): load the corrected world (+ edits) and drive a
 *  LIVE agent FORWARD from the mid-episode URL in the gym, then verify. Slow +
 *  (for LLM agents) stochastic. Submits to the job queue and polls to the driven
 *  verdict. onStatus fires on each phase change. */
export async function driveForwardGym(
  body: {
    taskId: string;
    seed: number;
    worldState?: Record<string, unknown>;
    edits?: Record<string, unknown>;
    resumeUrl: string;
    resumeStep?: number;
    agent?: string;
    sessionId?: string; // the human session driving this correction → verdict isolation
    correction?: string; // reviewer's natural-language instruction, injected into the agent
  },
  opts?: { onStatus?: (s: GymJob["status"]) => void; intervalMs?: number; timeoutMs?: number },
): Promise<{ reward: number; steps: Step[]; gymResume?: GymResume } | null> {
  const out = await post<{ jobId: string }>("/api/gym/resume-run", body);
  const jobId = out?.jobId;
  if (!jobId) return null;
  const interval = opts?.intervalMs ?? 2000;
  const deadline = Date.now() + (opts?.timeoutMs ?? 320_000);
  let last: GymJob["status"] | null = null;
  while (Date.now() < deadline) {
    await sleep(interval);
    const j = await pollGymJob(jobId);
    if (!j) continue;
    if (j.status !== last) { last = j.status; opts?.onStatus?.(j.status); }
    if (j.status === "done") {
      const review = j.review as { gymReward?: number; steps?: Step[]; gymResume?: GymResume } | undefined;
      // Carry the RESUMED world forward: the next correction must continue from
      // where this one ended, so iterations compound instead of re-anchoring to
      // the original run's end-state.
      return { reward: review?.gymReward ?? 0, steps: review?.steps ?? [], gymResume: review?.gymResume };
    }
    if (j.status === "error") return null;
  }
  return null;
}

/** Persist a gym drive-forward branch on the session so the fork round-trips
 *  (rerun_from + the branch restore via the open-session snapshot). */
export function rerunGymBranch(sid: string, body: { fromStep: number; steps: Step[]; mode?: string; correction?: string }): Promise<{ fromStep: number; mode: string; steps: Step[] } | null> {
  return post<{ fromStep: number; mode: string; steps: Step[] }>(`/api/sessions/${sid}/rerun-gym`, body);
}

/** Resume a gym task from its corrected state: load the captured world (+ optional
 *  dot-path edits) into the gym and replay the trajectory → REAL milestone verdict. */
export async function resumeGymReview(body: {
  taskId: string;
  seed: number;
  worldState?: Record<string, unknown>;
  urlTrail: string[];
  finalUrl: string;
  edits?: Record<string, unknown>;
}): Promise<ResumeResult | null> {
  return post<ResumeResult>("/api/gym/resume", body);
}

/** Replay the LATEST persisted gym run for a task from the DB — no live agent, so
 *  reopening a task is instant AND shows the SAME run the annotator was reviewing
 *  (a saved correction fork restores onto the identical trajectory). Returns null
 *  when the task was never reviewed (→ caller runs a fresh agent), which persists
 *  one for next time. */
/**
 * The task, ready for a HUMAN to do it — instant, no agent.
 *
 * Selecting a task used to trigger a live model run and make the annotator wait
 * for it, then review its attempt. The annotator now performs the task
 * themselves, so there is nothing to run: this returns the brief with an empty
 * step list, and their own interactions fill it in as they work.
 */
export async function getManualReview(taskId: string): Promise<ReviewData | null> {
  try {
    const res = await fetch(`/api/gym/tasks/${encodeURIComponent(taskId)}/manual-review`, { credentials: "include" });
    if (!res.ok) return null;
    return mapPayload((await res.json()) as ReviewPayload);
  } catch {
    return null;
  }
}

export async function getPersistedGymReview(taskId: string): Promise<ReviewData | null> {
  try {
    const res = await fetch(`/api/gym/tasks/${encodeURIComponent(taskId)}/persisted-review`, { credentials: "include" });
    if (!res.ok) return null; // 404 = never reviewed → run fresh
    return mapPayload((await res.json()) as ReviewPayload);
  } catch {
    return null;
  }
}

/** Enqueue a real gym run; returns the jobId to poll, or null if unreachable.
 *  `brief` (annotator prompt edit) re-drives the whole run under the new prompt. */
export async function startGymReview(taskId: string, agent = "oracle", seed = 0, brief?: string): Promise<string | null> {
  const out = await post<{ jobId: string }>(`/api/gym/tasks/${taskId}/run-review`, { agent, seed, ...(brief ? { brief } : {}) });
  return out?.jobId ?? null;
}

/** One poll of a gym job. */
export async function pollGymJob(jobId: string): Promise<GymJob | null> {
  try {
    const res = await fetch(`/api/gym/jobs/${encodeURIComponent(jobId)}`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as GymJob;
  } catch {
    return null;
  }
}

/** Start a real gym run and poll to completion (the run is now OFF the request
 *  path, so a slow browser run can't time out the POST). onStatus fires on each
 *  phase change for the loading UI. */
export async function runGymReview(
  taskId: string,
  agent = "oracle",
  seed = 0,
  opts?: { onStatus?: (s: GymJob["status"]) => void; intervalMs?: number; timeoutMs?: number; brief?: string },
): Promise<ReviewData | null> {
  const jobId = await startGymReview(taskId, agent, seed, opts?.brief);
  if (!jobId) return null;
  const interval = opts?.intervalMs ?? 1500;
  const deadline = Date.now() + (opts?.timeoutMs ?? 300_000);
  let last: GymJob["status"] | null = null;
  while (Date.now() < deadline) {
    await sleep(interval);
    const j = await pollGymJob(jobId);
    if (!j) continue; // transient blip — keep polling
    if (j.status !== last) { last = j.status; opts?.onStatus?.(j.status); }
    if (j.status === "done") return j.review ? mapPayload(j.review) : null;
    if (j.status === "error") return null;
  }
  return null; // client-side timeout guard
}

// ---- iteration history -----------------------------------------------------

/** One correction round the annotator made (oldest → newest). */
export interface HistoryRound {
  round: number;
  branchId: string;
  fromStep: number;
  mode: string;
  correction: string;
  steps: Step[];
  stepCount: number;
  at: string;
}

/** Every correction round on this session — how the annotator steered the agent.
 *  The main view only restores the LATEST round; this is the full chain. */
export async function fetchSessionHistory(sid: string): Promise<HistoryRound[]> {
  try {
    const res = await fetch(`/api/sessions/${sid}/history`, { credentials: "include" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return ((await res.json()) as { rounds?: HistoryRound[] }).rounds ?? [];
  } catch {
    return [];
  }
}
