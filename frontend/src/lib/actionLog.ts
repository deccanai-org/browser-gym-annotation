/**
 * Reading the trajectory as it builds, and proving it.
 *
 * Not built on lib/api.ts: those helpers collapse every failure into null, and
 * here the difference between "no steps yet" and "we could not reach the server"
 * is exactly what an annotator needs to see — the first is normal, the second
 * means their work may not be landing.
 */
import type { LoggedStep } from "../features/live-gym/ActionLog";

export interface LiveStepsResult {
  ok: boolean;
  steps: LoggedStep[];
  error?: string;
}

interface RawStep {
  stepId?: string;
  id?: string;
  actionType?: string;
  action_type?: string;
  description?: string;
  replayState?: string;
  replay_state?: string;
  screenshotUrl?: string;
  screenshot_url?: string;
  tabId?: string;
  tab_id?: string;
  worldDelta?: unknown;
  stateChange?: string;
  deltaSpan?: string[];
}

function normalise(raw: RawStep[]): LoggedStep[] {
  return raw.map((s, i) => ({
    stepId: String(s.stepId ?? s.id ?? i),
    index: i,
    actionType: String(s.actionType ?? s.action_type ?? ""),
    description: String(s.description ?? ""),
    replayState: s.replayState ?? s.replay_state ?? "unverified",
    screenshotUrl: s.screenshotUrl ?? s.screenshot_url ?? "",
    tabId: s.tabId ?? s.tab_id ?? "",
    // What this step changed in the world. `undefined` means NOT OBSERVED —
    // deliberately distinct from an object with `changed: false`, which means
    // observed and nothing moved.
    worldDelta: (s.worldDelta as LoggedStep["worldDelta"]) ?? undefined,
    stateChange: s.stateChange ?? "",
    deltaSpan: s.deltaSpan ?? [],
  }));
}

/** The head version's steps. Poll this while the annotator works. */
export async function fetchLiveSteps(attemptId: string, versionId: string): Promise<LiveStepsResult> {
  try {
    const res = await fetch(`/api/sessions/${attemptId}/versions/${versionId}/steps`, { credentials: "include" });
    if (!res.ok) return { ok: false, steps: [], error: `steps unavailable (${res.status})` };
    const body = (await res.json()) as { steps?: RawStep[] };
    return { ok: true, steps: normalise(body.steps ?? []) };
  } catch {
    return { ok: false, steps: [], error: "could not reach the server — your work may not be saving" };
  }
}

export interface CertifyResult {
  ok: boolean;
  certified: number;
  firstFailureAt: number | null;
  steps: { stepId: string; state: string; error: string }[];
  error?: string;
}

/** Prove the steps replay — in a SCRATCH world, so the annotator's own is untouched. */
export async function certifyTrajectory(attemptId: string, versionId?: string): Promise<CertifyResult> {
  try {
    const res = await fetch(`/api/sessions/${attemptId}/certify`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      credentials: "include",
      body: JSON.stringify(versionId ? { versionId } : {}),
    });
    const body = (await res.json()) as CertifyResult;
    if (!res.ok) return { ok: false, certified: 0, firstFailureAt: null, steps: [], error: String((body as unknown as { detail?: string }).detail ?? res.status) };
    return body;
  } catch {
    return { ok: false, certified: 0, firstFailureAt: null, steps: [], error: "could not reach the server" };
  }
}

/** The attempt's head version — the trajectory its steps land on.
 *
 *  Resolved here rather than threaded down through the review screen: the head
 *  is server state (it moves on a fork or a select), so asking is both simpler
 *  and less likely to go stale than mirroring it in component state. */
export async function fetchHeadVersionId(attemptId: string): Promise<string | null> {
  try {
    const res = await fetch(`/api/sessions/${attemptId}/versions`, { credentials: "include" });
    if (!res.ok) return null;
    const body = (await res.json()) as { headVersionId?: string | null };
    return body.headVersionId ?? null;
  } catch {
    return null;
  }
}
