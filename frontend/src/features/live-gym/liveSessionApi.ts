/**
 * The attempt's live browser session, as minted by the annotator backend.
 *
 * The backend mints it rather than this client for two reasons that both fail
 * silently if got wrong. It is the only side that knows which gym this attempt
 * runs against (its own isolated workspace endpoint, else settings.gym_url), and
 * it is the only side that can say who the signed-in annotator is. The live
 * service closes the stream 4401 when a ticket's owner is not the owner its
 * session was minted for, and a 4401 looks exactly like a stream that never
 * started — so neither the URL nor the owner is a client input here.
 *
 * Not built on lib/api.ts: those helpers collapse every failure into null, and
 * "the live browser service is unreachable" (409) is the one failure an
 * annotator can act on themselves. Collapsed into null it reads as a broken
 * pane, and they go looking for a bug that isn't there.
 */
import type { Viewport } from "../../lib/liveBrowser";

export interface LiveSession {
  sessionId: string;
  ticket: string;
  viewport: Viewport;
  /** Where the server opened the browser — the attempt's own workspace
   *  endpoint, so an isolated workspace is honoured. */
  url: string;
  /** Whether this attempt got its OWN gym, or fell back to the shared one. */
  isolated?: boolean;
  /** Where the world in that gym came from on this open:
   *  - "preserved" — the work already in there was kept
   *  - "seeded"    — reset to the task's seed state
   *  - "shared"    — not a gym task, or the shared gym
   *  Surfaced because an annotator cannot otherwise tell whether the cart they
   *  spent an hour filling is still there except by going to look for it. */
  world?: "preserved" | "seeded" | "shared";
  /** For a FORK: how far the branch's prefix was rebuilt into the world, so the
   *  annotator does not have to re-perform it by hand. null when there was no
   *  prefix to rebuild (an unforked attempt). `partial` means the rebuild stopped
   *  short — the world is at `done` of `total`, and `reason` says why. */
  restore?: RestoreProgress | null;
}

export interface RestoreProgress {
  done: number;
  total: number;
  partial: boolean;
  reason: string;
}

export type LiveFailureKind =
  | "unavailable" // 409 — the backend could not reach the live browser service
  | "missing" // 404 — including someone else's attempt; ownership is never disclosed as 403
  | "denied" // 401 — the session cookie is gone
  | "offline" // the request never reached the server
  | "error";

export interface LiveFailure {
  ok: false;
  kind: LiveFailureKind;
  status: number;
  message: string;
}

export type LiveResult<T> = { ok: true; value: T } | LiveFailure;

const KIND: Record<number, LiveFailureKind> = {
  401: "denied",
  403: "denied",
  404: "missing",
  409: "unavailable",
};

async function request<T>(url: string, init?: RequestInit): Promise<LiveResult<T>> {
  let res: Response;
  try {
    res = await fetch(url, { credentials: "include", ...init });
  } catch {
    return { ok: false, kind: "offline", status: 0, message: "Cannot reach the server." };
  }
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = body?.detail;
    return {
      ok: false,
      kind: KIND[res.status] ?? "error",
      status: res.status,
      // The 409 detail names the live service and why it could not be reached;
      // a generic "request failed" would hide the only actionable part.
      message: typeof detail === "string" && detail ? detail : `Request failed (${res.status}).`,
    };
  }
  try {
    return { ok: true, value: (await res.json()) as T };
  } catch {
    return { ok: false, kind: "error", status: res.status, message: "The server sent a response we could not read." };
  }
}

const at = (attemptId: string) => `/api/sessions/${encodeURIComponent(attemptId)}`;

/**
 * Open this attempt's live browser, or re-attach to the one it already has.
 *
 * Called before every connect, including when `currentLiveBrowser` has just
 * said a session is open: tickets are short-lived (LIVE_TICKET_TTL_S), and a
 * socket opened with an expired one closes 4401 and streams nothing. The server
 * re-tickets the SAME browser, so re-attaching never costs a second Chromium.
 */
export function attachLiveBrowser(attemptId: string): Promise<LiveResult<LiveSession>> {
  // Empty on purpose — see the note at the top on who chooses the URL and owner.
  return request<LiveSession>(`${at(attemptId)}/live`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
}

/**
 * The live browser currently open for this attempt, or null.
 *
 * A page reload never runs the pane's cleanup, so the browser from the previous
 * mount is still running. This is how the next mount finds it instead of
 * orphaning it and opening another alongside. This is a probe, not a handle:
 * whatever ticket it carries can age out between here and the moment the
 * annotator actually opens the view, so connecting goes through
 * `attachLiveBrowser` for a ticket minted at that moment.
 */
export async function currentLiveBrowser(attemptId: string): Promise<LiveResult<LiveSession | null>> {
  const res = await request<{ session: LiveSession | null }>(`${at(attemptId)}/live`);
  return res.ok ? { ok: true, value: res.value.session ?? null } : res;
}

/**
 * Throw this attempt's world away and rebuild it from the task seed.
 *
 * The deliberate counterpart to preserving a world across a reopen. Closing and
 * reopening the pane used to start an annotator over — implicitly, and every
 * time. Now that reopening keeps their work, this is the way out for someone who
 * has driven their world into a corner.
 */
export function resetLiveWorld(
  attemptId: string,
): Promise<LiveResult<{ reset: boolean; world: LiveSession["world"]; restore: RestoreProgress | null }>> {
  return request<{ reset: boolean; world: LiveSession["world"]; restore: RestoreProgress | null }>(
    `${at(attemptId)}/live/reset-world`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    },
  );
}

/**
 * Close this attempt's live browser.
 *
 * `keepalive` because the caller is an unmount: a request the browser cancels
 * as the view goes away leaves one Chromium running per task the annotator
 * opened, and nothing later notices.
 */
export function closeLiveBrowser(attemptId: string): Promise<LiveResult<{ closed: boolean }>> {
  return request<{ closed: boolean }>(`${at(attemptId)}/live/close`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{}",
    keepalive: true,
  });
}
