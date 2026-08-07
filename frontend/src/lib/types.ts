import type { ActionType, VerifierLevel } from "../ds/tokens";

export type AppKey = "shop" | "market" | "calendar" | "mail" | "food";

/** One recorded agent step. */
export interface Step {
  idx: number;
  type: ActionType;
  tabId: string;
  description: string;
  /** captured-snapshot key for this step's page (served at /api/snapshots/{key}). */
  snapshot?: string;
  /** full image URL for a captured screenshot (gym tasks, M8) — rendered as <img>. */
  image?: string;
  /** page-level error message shown as the red frame banner (error steps, §2.1). */
  errorMsg?: string;
  /** the page this step landed on (gym runs) — the resume point when correcting it,
   *  so a correction on a re-run branch step resumes from THAT page, not the
   *  original run's final URL. */
  url?: string;
}

export interface Metric {
  value: string;
  label: string;
  tone?: "default" | "error" | "success";
}

/** A verifier check. */
export interface Verifier {
  id: string;
  level: VerifierLevel;
  assertion: string;
  code: string;
  failsUntilCorrected?: boolean;
  placeholder?: boolean;
  /** Executable check IR (M5) — evaluated by the backend engine. */
  check?: Record<string, unknown>;
  /** Real milestone result for gym tasks (M8) — "pass" | "fail". */
  gymResult?: string;
}

// ---- API shape (returned by the backend) ----------------------------------

export interface ApiTab {
  id: string;
  app: AppKey;
  title: string;
  host: string;
}
export interface ApiSite {
  host: string;
  app: AppKey;
}
export interface ApiTask {
  id: string;
  priority: "High" | "Medium" | "Low";
  title: string;
  meta: string;
  prompt: string;
  startState: { summary: string; url: string };
  constraints: string[];
  allowedSites: ApiSite[];
  runSummary: Metric[];
}
/** Everything needed to resume a gym episode from a corrected state (M-resume). */
export interface GymResume {
  seed: number;
  urlTrail: string[];
  finalUrl: string;
  worldState?: Record<string, unknown>;
  /** The world AFTER each step. Correcting step N must resume from
   *  worldTrail[N-1] — resuming from `worldState` (the run's FINAL world) replays
   *  a state that already contains the effects of every later step. */
  worldTrail?: (Record<string, unknown> | null)[];
}
export interface ReviewPayload {
  task: ApiTask;
  tabs: ApiTab[];
  steps: Step[];
  correctionSeed: string;
  correctedTail: Step[];
  verifiers: Verifier[];
  source?: "fixture" | "gym";
  gymReward?: number;
  gymResume?: GymResume;
}

/** A row in the task queue (from GET /api/tasks). */
export interface TaskListItem {
  id: string;
  title: string;
  priority: "High" | "Medium" | "Low";
  meta: string;
  index: number;
  total: number;
  /** "gym" tasks (the breakers) load via a live gym run; "fixture" demos load a
   *  baked review payload. Absent → treated as fixture for back-compat. */
  source?: "fixture" | "gym";
  prompt?: string;
}

// ---- Domain shape (API mapped → colors resolved for rendering) ------------

export interface Tab {
  id: string;
  title: string;
  host: string;
  color: string;
}
export interface Task {
  id: string;
  priority: "High" | "Medium" | "Low";
  title: string;
  meta: string;
  prompt: string;
  startState: { summary: string; url: string };
  constraints: string[];
  // `app` rides along because the gym task picker names apps rather than
  // hosts, and a chip with no label is just a coloured dot.
  allowedSites: { host: string; app?: string; color: string }[];
  runSummary: Metric[];
}
export interface ReviewData {
  task: Task;
  tabs: Tab[];
  steps: Step[];
  correctionSeed: string;
  correctedTail: Step[];
  verifiers: Verifier[];
  source?: "fixture" | "gym";
  gymReward?: number;
  gymResume?: GymResume;
}

/** Review-flow state machine (mirrors the design's linear gate chain). */
export interface ReviewState {
  data: ReviewData;
  step: number;
  activeTabId: string;
  playing: boolean;
  verifiedThrough: number;
  stepsApproved: boolean;
  verifiersGenerated: boolean;
  benchmarkRun: boolean;
  submitted: boolean;
  rerunFrom: number | null;
  overrides: Record<string, boolean>;
  activeLevel: VerifierLevel;
  added: Verifier[];
  /** in-place edits to any verifier (generated or added), keyed by id. */
  edits: Record<string, { assertion: string; code: string }>;
  /** real per-verifier results from the last benchmark run (M5), keyed by id. */
  results: Record<string, string>;
  /** server-computed corrected branch (M6); null → offline fixture tail. */
  branchTail: Step[] | null;
  /** The steps KEPT before the latest correction point. Captured from the trace the
   *  annotator was actually looking at, so correcting a step inside an earlier
   *  re-run branch keeps that branch's steps instead of slicing the (shorter)
   *  canonical run and silently dropping them. null → fork off the canonical. */
  baseSteps: Step[] | null;
  /** how the branch was produced (M6b): "agent" | "deterministic" | null. */
  rerunMode: string | null;
  /** real gym verdict after a resume-from-corrected-state (M-resume); overrides
   *  gymReward once a gym task has been re-verified. */
  gymResumeReward: number | null;
  /** server-confirmed submission (authoritative reward/kind) — set only after the
   *  submit POST succeeds, so the UI never shows a golden that wasn't written. */
  /** The reward the SERVER computed and stored for the last benchmark. What the
   *  annotator sees must be what actually ships. */
  /** Verifiers whose compiled check IR was invalidated by an edit — they fail
   *  closed rather than scoring the pre-edit condition. */
  staleChecks: Record<string, boolean>;
  serverReward: number | null;
  serverSubmission: { reward: number; kind: string } | null;
  /** an inline error if the submit POST failed (so it's never silently lost). */
  submitError: string | null;
}
