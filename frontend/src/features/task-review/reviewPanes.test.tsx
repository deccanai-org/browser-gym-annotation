import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "../../ds";
import type { ReviewData } from "../../lib/types";
import type { LiveSession } from "../live-gym/liveSessionApi";
import { LineagePanel, ReviewScreen, ReviewSurface } from "./TaskReview";

/**
 * These render the SCREEN, not the pieces in isolation: a pane that is built,
 * tested and never mounted is indistinguishable from one that does not exist,
 * and that is the failure this file guards.
 */

const data: ReviewData = {
  task: {
    id: "M37_false_overcharge",
    priority: "High",
    title: "Dispute a charge that was never made",
    meta: "E-commerce · breaker",
    prompt: "Refund the duplicate charge on order 8812.",
    startState: { summary: "Signed in as Alice", url: "https://shop.gym.local" },
    constraints: [],
    allowedSites: [{ host: "shop.gym.local", color: t.primary6 }],
    runSummary: [],
  },
  tabs: [{ id: "tab-1", title: "ShopGym", host: "shop.gym.local", color: t.primary6 }],
  steps: [{ idx: 1, type: "click", tabId: "tab-1", description: "open order 8812" }],
  correctionSeed: "",
  correctedTail: [],
  verifiers: [],
  source: "gym",
};

const nav = {
  index: 0,
  total: 1,
  onPrev: () => {},
  onNext: () => {},
  onSkip: () => {},
  onBrowseGym: () => {},
  onOpenQa: () => {},
  // The signed-in identity. It is what the ticket's owner has to be, so a
  // screen rendered without one can never drive a live browser.
  annotator: { id: "a1", email: "ann@deccan.ai", role: "annotator", displayName: "Ann", avatarHue: 210, lastLoginAt: null },
  onOpenProfile: () => {},
};

const live: LiveSession = {
  sessionId: "live-7",
  ticket: "tkt-fresh",
  viewport: { width: 1280, height: 800 },
  url: "http://127.0.0.1:9411/",
};

describe("the review screen", () => {
  const html = renderToStaticMarkup(<ReviewScreen data={data} nav={nav} startFresh={false} onStartNew={() => {}} />);

  it("opens on the workspace, never on a recorded run", () => {
    // The annotator performs the task themselves. There is no agent attempt to
    // replay, so landing anywhere but the gym would be landing on a surface
    // that has nothing to show — and the old replay pane would happily render
    // somebody else's steps if a stale payload ever reached it.
    expect(html).toContain("Do the task");
    expect(html, "the replay pane is gone, not merely hidden").not.toContain("captured frame");
  });

  it("has no replay/live toggle to get lost behind", () => {
    // The gym IS the workspace. A toggle is what made it a place you had to go
    // and find, which is exactly how it stayed invisible.
    expect(html).not.toContain("Live browser");
    expect(html).not.toContain("Replay");
  });

  it("mounts the version lineage next to the trajectory it describes", () => {
    expect(html).toContain("Version lineage");
    expect(html).toContain("Steps in this version");
  });

  it("leaves out the sections it has nothing to put in", () => {
    // A gym task carries no constraints, and no run summary until something has
    // run. Both headings used to render regardless, so the brief showed two bare
    // labels and an empty grey box — which reads as "this failed to load", not
    // as "there is nothing here".
    expect(data.task.constraints).toHaveLength(0);
    expect(data.task.runSummary).toHaveLength(0);
    expect(html).not.toContain("Constraints");
    expect(html).not.toContain("Run summary");
  });

  it("labels an allowed site even when the payload names apps, not hosts", () => {
    // The gym task picker sends {app, title} while the brief reads `host`, so
    // every chip rendered as a bare coloured dot with no text beside it.
    const byApp: ReviewData = {
      ...data,
      task: { ...data.task, allowedSites: [{ host: "", app: "mail", color: t.primary6 }] },
    };
    const out = renderToStaticMarkup(
      <ReviewScreen data={byApp} nav={nav} startFresh={false} onStartNew={() => {}} />,
    );
    expect(out).toContain("mail");
  });
});

describe("the workspace surface", () => {
  it("hands the live pane the session that was minted for this attempt", () => {
    // The pane prints the session it is streaming: if the surface dropped the
    // minted session the pane would silently fall back to its own empty state.
    const html = renderToStaticMarkup(
      <ReviewSurface session={live} attemptId="att-1" owner="ann@deccan.ai" />,
    );
    expect(html).toContain("live-7 · 1280×800");
  });

  it("shows the trajectory beside the gym, so capture is visible while working", () => {
    // A lost interaction found at the END of a task is an hour thrown away.
    const html = renderToStaticMarkup(<ReviewSurface session={live} attemptId="att-1" />);
    expect(html).toContain("Trajectory");
    expect(html).toContain("Nothing recorded yet");
  });

  it("records the annotator's interactions against the review session", () => {
    // Without the attempt id the pane drives the browser but writes nothing, so
    // the interaction never reaches the trajectory.
    const withAttempt = renderToStaticMarkup(<ReviewSurface session={live} attemptId="att-1" />);
    const without = renderToStaticMarkup(<ReviewSurface session={live} attemptId={null} />);

    expect(withAttempt).toContain("Recording interactions");
    expect(without).toContain("Not recording");
  });

  it("says the gym is coming up rather than showing an empty frame", () => {
    const html = renderToStaticMarkup(<ReviewSurface session={null} attemptId="att-1" opening />);
    expect(html).toContain("Opening the gym…");
  });

  it("replaces the workspace when the gym refused to start", () => {
    // A live pane with no stream accepts every click and records none of them,
    // which is strictly worse than showing nothing: the annotator works for an
    // hour and the trajectory is empty.
    const html = renderToStaticMarkup(
      <ReviewSurface session={null} attemptId="att-1" error="the gym pool is full" />,
    );
    expect(html).toContain("The gym could not be opened");
    expect(html).toContain("the gym pool is full");
    expect(html).not.toContain("Recording interactions");
  });
});

// --------------------------------------------------------------------------- the mounted screen

/** The socket the pane opens for itself. jsdom would otherwise dial the real
 *  live service and answer with a close the pane then tries to recover from. */
class SilentSocket {
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {}
  send(): void {}
  close(): void {}
}

const SNAPSHOT = {
  sessionId: "att-1",
  taskExternalId: data.task.id,
  status: "draft",
  rerunFrom: null,
  reviewedThrough: 0,
  suite: null,
  lastBenchmark: null,
  branch: null,
  submission: null,
};

/** Answers the whole review screen's traffic and records it, so a test can
 *  assert which calls the annotator's clicks actually produced. */
function stubApi(): string[] {
  const calls: string[] = [];
  vi.stubGlobal("WebSocket", SilentSocket);
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = String(url);
    calls.push(`${method} ${path}`);
    let body: unknown = {};
    if (path.endsWith("/sessions") && method === "POST") body = SNAPSHOT;
    else if (path.endsWith("/live/close")) body = { closed: true };
    else if (path.endsWith("/live")) body = method === "POST" ? live : { session: null };
    else if (path.endsWith("/versions")) body = { attemptId: "att-1", revision: 0, headVersionId: null, agentCallCount: 0, versions: [], verdicts: {} };
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("entering and leaving the live view", () => {
  const mount = async () => {
    const calls = stubApi();
    render(<ReviewScreen data={data} nav={nav} startFresh={false} onStartNew={() => {}} />);
    // The attempt has to be saved before anything can be opened against it.
    await screen.findByText(/Autosaved/);
    return calls;
  };

  it("opens the gym on its own and streams it into the pane — no click", async () => {
    // The workspace is the whole job of the screen, so it must come up by
    // itself. Requiring a toggle is what kept it invisible.
    const calls = await mount();
    await screen.findByText(/live-7 · 1280×800/);
    expect(calls, "the attempt must mint a session on load, not render an empty pane")
      .toContain("POST /api/sessions/att-1/live");
  });

  it("closes the browser when the annotator leaves the task", async () => {
    // A live browser is a real Chromium; leaving without closing leaks one per
    // task opened. Nothing to click now — the auto-open above is what launched it.
    const calls = await mount();
    await screen.findByText(/live-7 · 1280×800/);

    cleanup();
    await waitFor(() => expect(calls).toContain("POST /api/sessions/att-1/live/close"));
  });

  it("looks for a browser the previous page left open before opening one", async () => {
    // A reload never runs the cleanup above, so without this probe the orphan
    // keeps streaming to nobody until the live service is restarted.
    const calls = await mount();
    expect(calls).toContain("GET /api/sessions/att-1/live");
  });

  it("never baselines a gym attempt from the canonical agent run", async () => {
    // v1 is now minted empty by the live open (ensure_manual_root), and the
    // annotator's own actions fill it. The old baseline call cloned the breaker's
    // recorded AGENT run into the attempt — 13 steps nobody took — and, racing the
    // multi-second live open, won and became the head. That is the exact bug the
    // user saw. It must not fire on open, ever.
    const calls = await mount();
    // Give any stray effect a tick to fire, then assert it did not.
    await waitFor(() => expect(calls).toContain("POST /api/sessions/att-1/live"));
    expect(calls.filter((c) => c.endsWith("/versions/baseline"))).toEqual([]);
  });
});

describe("the lineage panel", () => {
  it("explains what v1 is instead of showing an empty rail", () => {
    // With no backend there is no lineage to draw, and an annotator staring at
    // a blank card cannot tell that from a broken one.
    const html = renderToStaticMarkup(<LineagePanel sessionId={null} isGym />);
    expect(html).toContain("Nothing branched yet");
    expect(html).toContain("This version has no steps yet.");
  });

  it("does not describe v1 as an agent run", () => {
    // This is a human-do platform: the annotator performs the task and their own
    // actions ARE v1. The panel used to open on "v1 is the canonical agent run
    // this attempt annotates", "3 of 3 agent runs left", and "an agent run
    // finishes as a candidate" — every one of them false on the screen it was
    // shown on, and left over from when this reviewed recorded agent runs.
    const html = renderToStaticMarkup(<LineagePanel sessionId={null} isGym />);
    expect(html).not.toContain("agent run");
    expect(html).not.toContain("agent runs left");
  });

  it("does not offer to create a baseline it cannot save", () => {
    const html = renderToStaticMarkup(<LineagePanel sessionId={null} isGym />);
    expect(html, "an offline session has nowhere to write v1").not.toContain("Create baseline v1");
  });
});
