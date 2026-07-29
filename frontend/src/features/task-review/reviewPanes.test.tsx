import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("opens on the recorded run", () => {
    // An annotator opening a breaker is there to review the attempt that
    // already happened; a live browser on load would also start a Chromium for
    // every task they merely page past.
    expect(html).toContain("captured frame");
    expect(html, "the live pane must not be mounted until it is asked for").not.toContain("No live browser attached");
  });

  it("offers the live browser as the other view of the same box", () => {
    expect(html).toContain("Live browser");
    expect(html).toContain("Replay");
  });

  it("mounts the version lineage next to the run it describes", () => {
    expect(html).toContain("Version lineage");
    expect(html).toContain("Steps in this version");
  });
});

describe("the review surface", () => {
  it("hands the live pane the session that was minted for this attempt", () => {
    // The pane prints the session it is streaming: if the toggle dropped the
    // minted session the pane would silently fall back to its own empty state.
    const html = renderToStaticMarkup(
      <ReviewSurface view="live" session={live} attemptId="att-1" owner="ann@deccan.ai" replay={<div>RECORDED RUN</div>} />,
    );
    expect(html).toContain("live-7 · 1280×800");
    expect(html, "the replay is not rendered underneath the live view").not.toContain("RECORDED RUN");
  });

  it("records the annotator's interactions against the review session", () => {
    // Without the attempt id the pane drives the browser but writes nothing, so
    // the interaction never reaches the trajectory.
    const withAttempt = renderToStaticMarkup(
      <ReviewSurface view="live" session={live} attemptId="att-1" replay={<div />} />,
    );
    const without = renderToStaticMarkup(<ReviewSurface view="live" session={live} attemptId={null} replay={<div />} />);

    expect(withAttempt).toContain("Recording interactions");
    expect(without).toContain("Not recording");
  });

  it("keeps the recorded run as the default view", () => {
    const html = renderToStaticMarkup(
      <ReviewSurface view="replay" session={null} attemptId="att-1" replay={<div>RECORDED RUN</div>} />,
    );
    expect(html).toContain("RECORDED RUN");
    expect(html).not.toContain("No live browser attached");
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

const LIVE_TAB = /drive the same page/;
const REPLAY_TAB = /screenshots of the attempt/;

describe("entering and leaving the live view", () => {
  const mount = async () => {
    const calls = stubApi();
    render(<ReviewScreen data={data} nav={nav} startFresh={false} onStartNew={() => {}} />);
    // The attempt has to be saved before anything can be opened against it.
    await screen.findByText(/Autosaved/);
    return calls;
  };

  it("opens a browser for this attempt and streams it into the pane", async () => {
    const calls = await mount();
    fireEvent.click(screen.getByTitle(LIVE_TAB));

    await screen.findByText(/live-7 · 1280×800/);
    expect(calls, "the toggle must mint a session, not render an empty pane").toContain("POST /api/sessions/att-1/live");
  });

  it("KEEPS the browser when the annotator glances at the replay", async () => {
    // This used to close it, and that made the toggle quietly destructive: the
    // server reseeds the gym on a fresh open, so checking a step on the replay
    // and coming back reset an hour of hand-built world to the task seed. The
    // Chromium-per-view leak this once guarded against is handled where it
    // actually belongs — leaving the task, covered by the next test.
    const calls = await mount();
    fireEvent.click(screen.getByTitle(LIVE_TAB));
    await screen.findByText(/live-7 · 1280×800/);

    fireEvent.click(screen.getByTitle(REPLAY_TAB));
    // The pane is unmounted (the socket drops) but the browser is not given back.
    await waitFor(() => expect(screen.queryByText(/live-7/)).toBeNull());
    expect(calls, "the browser must survive a look at the replay").not.toContain(
      "POST /api/sessions/att-1/live/close",
    );
  });

  it("re-mints a ticket when the annotator comes back to the live pane", async () => {
    // The browser survived, but its ticket does not: tickets expire, and a socket
    // opened with a stale one closes 4401, which is terminal in the client. So
    // returning must still go through the open call — the server re-tickets the
    // SAME browser rather than starting a second one.
    const calls = await mount();
    fireEvent.click(screen.getByTitle(LIVE_TAB));
    await screen.findByText(/live-7 · 1280×800/);
    fireEvent.click(screen.getByTitle(REPLAY_TAB));
    await waitFor(() => expect(screen.queryByText(/live-7/)).toBeNull());

    const before = calls.filter((c) => c === "POST /api/sessions/att-1/live").length;
    fireEvent.click(screen.getByTitle(LIVE_TAB));
    await screen.findByText(/live-7 · 1280×800/);
    expect(calls.filter((c) => c === "POST /api/sessions/att-1/live").length).toBe(before + 1);
  });

  it("closes the browser when the annotator leaves the task", async () => {
    const calls = await mount();
    fireEvent.click(screen.getByTitle(LIVE_TAB));
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

  it("materializes the canonical v1 when a gym attempt opens", async () => {
    // There is no lineage to read, and nothing to fork from, until v1 exists.
    const calls = await mount();
    await waitFor(() => expect(calls).toContain("POST /api/sessions/att-1/versions/baseline"));
  });
});

describe("the lineage panel", () => {
  it("explains what v1 is instead of showing an empty rail", () => {
    // With no backend there is no lineage to draw, and an annotator staring at
    // a blank card cannot tell that from a broken one.
    const html = renderToStaticMarkup(<LineagePanel sessionId={null} isGym />);
    expect(html).toContain("No lineage yet");
    expect(html).toContain("This version has no steps yet.");
  });

  it("does not offer to create a baseline it cannot save", () => {
    const html = renderToStaticMarkup(<LineagePanel sessionId={null} isGym />);
    expect(html, "an offline session has nowhere to write v1").not.toContain("Create baseline v1");
  });
});
