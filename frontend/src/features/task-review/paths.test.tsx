import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "../../ds";
import { FORK_COPY } from "../../lib/versionsApi";
import type { ReviewData } from "../../lib/types";
import { ReviewScreen } from "./TaskReview";

/**
 * ONE correction path per attempt.
 *
 * Both systems used to be reachable from this screen. The retired one builds its
 * golden from the recorded run plus a branch and knows nothing about versions, so
 * an annotator who rejected a step and then pressed the old Submit shipped a
 * golden that still CONTAINED the rejected step. The backend refuses that now
 * (backend/app/api/sessions.py::_assert_no_unshipped_version_work), but a guard
 * an annotator can trip is a training hazard: they see two paths, pick one, and
 * learn a workflow that does not exist.
 *
 * So these tests are about what is on the SCREEN, driven through the real
 * component tree over a stubbed backend — a screen that renders both paths is
 * the defect, and no unit test of either path can see it.
 */

// --------------------------------------------------------------------------- the backend, as it actually answers

interface Call {
  method: string;
  path: string;
  body: unknown;
}

/**
 * One version row, serialized the way the server serializes it
 * (backend/app/api/versions.py::_describe, line 47).
 */
function version(over: Partial<{ id: string; versionNo: number; parentId: string | null; status: string; stepCount: number; kind: string }> = {}) {
  return {
    id: over.id ?? "V1",
    versionNo: over.versionNo ?? 1,
    parentId: over.parentId ?? null,
    kind: over.kind ?? "agent_run",
    status: over.status ?? "candidate",
    revision: 0,
    producer: "gpt-5.1",
    forkBeforeStepId: null,
    forkCheckpointId: null,
    isHead: false, // recomputed by graphOf — see there
    stepCount: over.stepCount ?? 3,
    createdAt: "2026-07-23T10:00:00+00:00",
  };
}

type VersionRow = ReturnType<typeof version>;

/**
 * The lineage payload (backend/app/api/versions.py::list_versions, line 64).
 *
 * `isHead` is DERIVED here, never passed in, because the server derives it the
 * same way — `v.id == head_id`, line 58. A fixture free to set `headVersionId`
 * to V2 and `isHead` on V1 would let this suite pass against a screen that reads
 * one and renders the other, which is precisely the fake-that-skips-the-contract
 * failure this codebase has already shipped once.
 */
function graphOf(versions: VersionRow[], headVersionId: string | null) {
  return {
    attemptId: "att-1",
    revision: 0,
    headVersionId,
    agentCallCount: 0,
    versions: versions.map((v) => ({ ...v, isHead: v.id === headVersionId })),
    verdicts: {},
  };
}

/** The session snapshot open_session returns (backend/app/api/sessions.py::open_session). */
const SNAPSHOT = {
  sessionId: "att-1",
  taskExternalId: "M37_false_overcharge",
  status: "draft",
  rerunFrom: null,
  reviewedThrough: 0,
  suite: null,
  lastBenchmark: null,
  branch: null,
  submission: null,
};

/** What finalize hands back (backend/app/finalize.py, the dict at line 138). */
const SHIPPED = {
  submissionId: "sub-77",
  benchmarkRunId: "run-9",
  versionId: "V2",
  reward: 1,
  results: {},
  finalCheckpointId: "cp-3",
  replayed: true,
  steps: 2,
};

/**
 * Answer the whole screen's traffic and record it.
 *
 * Every route the screen touches is answered, so a missing stub can never be
 * mistaken for a path the UI declined to take — the assertions below are about
 * which calls the annotator's clicks produced, and a silent 404 would read as a
 * pass.
 */
function stubApi(routes: (path: string, method: string) => { status: number; body: unknown } | undefined = () => undefined): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = String(url);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ method, path, body });

    const routed = routes(path, method);
    if (routed) {
      if (routed.status === 0) return new Promise<Response>(() => {}); // deliberately unanswered
      return { ok: routed.status < 400, status: routed.status, json: async () => routed.body } as Response;
    }

    const reply = (b: unknown) => ({ ok: true, status: 200, json: async () => b }) as Response;
    if (path.endsWith("/sessions") && method === "POST") return reply(SNAPSHOT);
    if (path.endsWith("/live/close")) return reply({ closed: true });
    if (path.endsWith("/live")) return reply({ session: null });
    if (path.endsWith("/versions/baseline")) return reply(version());
    if (path.endsWith("/steps")) return reply({ versionId: "V1", versionNo: 1, steps: [] });
    if (path.endsWith("/versions")) return reply(graphOf([], null));
    if (path.endsWith("/runs")) return reply({ agentCallCount: 0, cap: null, runs: [] });
    if (path.endsWith("/run")) return reply({ results: {}, reward: 1, executed: 0, overridden: 0 });
    if (path.endsWith("/submit")) {
      return reply({ ...SNAPSHOT, status: "submitted", submission: { reward: 1, kind: "golden", override: false, at: "2026-07-23T11:00:00+00:00" } });
    }
    if (path.endsWith("/finalize")) return reply(SHIPPED);
    return reply({});
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

// --------------------------------------------------------------------------- the attempts under review

const task: ReviewData["task"] = {
  id: "M37_false_overcharge",
  priority: "High",
  title: "Dispute a charge that was never made",
  meta: "E-commerce · breaker",
  prompt: "Refund the duplicate charge on order 8812.",
  startState: { summary: "Signed in as Alice", url: "https://shop.gym.local" },
  constraints: [],
  allowedSites: [{ host: "shop.gym.local", color: t.primary6 }],
  runSummary: [],
};

const tabs = [{ id: "tab-1", title: "ShopGym", host: "shop.gym.local", color: t.primary6 }];

/** A breaker: the attempts that carry a version graph. */
const gymAttempt: ReviewData = {
  task,
  tabs,
  steps: [
    { idx: 1, type: "click", tabId: "tab-1", description: "open order 8812" },
    { idx: 2, type: "click", tabId: "tab-1", description: "start a refund for the wrong line" },
  ],
  correctionSeed: "",
  correctedTail: [],
  verifiers: [],
  source: "gym",
  gymReward: 0,
  gymResume: { seed: 7, worldState: {}, urlTrail: ["/"], finalUrl: "/orders/8812", worldTrail: [] },
};

/** A hand-authored fixture: never baselined, so it has no version rows and keeps
 *  the path it was started on. */
const legacyAttempt: ReviewData = { ...gymAttempt, source: "fixture", gymResume: undefined, gymReward: undefined };

const nav = {
  index: 0,
  total: 1,
  onPrev: () => {},
  onNext: () => {},
  onSkip: () => {},
  onBrowseGym: () => {},
  onOpenQa: () => {},
  annotator: { id: "a1", email: "ann@deccan.ai", role: "annotator", displayName: "Ann", avatarHue: 210, lastLoginAt: null },
  onOpenProfile: () => {},
};

const LEGACY_SUBMIT = /Approve & submit to dataset/;
const LEGACY_CORRECT = "Correct";
const VERSION_GUIDE = /How this attempt is corrected and shipped/;

/**
 * Mount the screen and wait until it has DECIDED which path this attempt is on.
 *
 * Without the wait, every "the other path is absent" assertion below could pass
 * against a screen that simply had not finished reading the lineage — the tests
 * would agree with the bug as readily as with the fix.
 */
const mount = async (data: ReviewData, opts?: { undecided?: boolean }) => {
  render(<ReviewScreen data={data} nav={nav} startFresh={false} onStartNew={() => {}} />);
  await screen.findByText(/Autosaved/); // the attempt is saved
  if (opts?.undecided) return;
  await waitFor(() => {
    const decided = screen.queryByText(LEGACY_CORRECT) ?? screen.queryByText(VERSION_GUIDE);
    expect(decided, "one correction path — and only then, the rest of the screen").not.toBeNull();
  });
};

/** The gates between opening an attempt and shipping it. Driven through the real
 *  controls rather than seeded into state — the point of these tests is what an
 *  annotator can reach by clicking. */
const approveSteps = () => fireEvent.click(screen.getByText(/Approve remaining|Approve all steps/));
const generateSuite = () => fireEvent.click(screen.getByText("Generate verifier suite"));
const runBenchmark = async () => {
  fireEvent.click(screen.getByText("Run benchmark"));
  await screen.findByText("Re-run benchmark");
};

const posted = (calls: Call[], suffix: string) => calls.filter((c) => c.method === "POST" && c.path.endsWith(suffix));

// --------------------------------------------------------------------------- an attempt with a version graph

describe("an attempt that has a version graph", () => {
  /** Head v2 hangs off v1 — exactly the shape the backend guard refuses to ship
   *  through the legacy submit. */
  const versioned = (status = "approved") => (path: string) =>
    path.endsWith("/versions")
      ? {
          status: 200,
          body: graphOf(
            [version({ id: "V1", versionNo: 1 }), version({ id: "V2", versionNo: 2, parentId: "V1", status, stepCount: 2, kind: "agent_correction" })],
            "V2",
          ),
        }
      : undefined;

  it("does not render the retired submit at all", async () => {
    // Not disabled: a control an annotator can see but must not use is still a
    // question they have to ask somebody, and the answer is a workflow that no
    // longer exists.
    stubApi(versioned());
    await mount(gymAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();

    expect(screen.queryByText(LEGACY_SUBMIT), "the legacy submit is absent, not greyed out").toBeNull();
    expect(screen.getByText(/ship it in step 3 below/), "and the corner it left says where shipping moved to").toBeDefined();
  });

  it("does not render the retired per-step correction, or the branch controls beside it", async () => {
    // Each of these writes a correction the version graph cannot contain, so the
    // work would survive as a branch that finalize then silently leaves out.
    stubApi(versioned());
    await mount(gymAttempt);

    expect(screen.queryByText(LEGACY_CORRECT), "the step card's Correct pill is gone").toBeNull();
    expect(screen.queryByText(/Drive forward/), "so is the drive-forward continuation").toBeNull();
    expect(screen.queryByText(/Edit state/), "and the world editor that re-verifies outside the graph").toBeNull();
    expect(screen.getByText("Verify"), "verifying a step belongs to both paths and stays").toBeDefined();
  });

  it("never calls the guarded submit route, on any click a shipping annotator makes", async () => {
    // The negation of backend/app/api/sessions.py::_assert_no_unshipped_version_work:
    // that guard fires when the head has a parent, and this attempt's head does.
    // If the UI can still reach /submit here, the 409 an annotator meets is the
    // product of this screen, not of the server protecting them.
    const calls = stubApi(versioned());
    await mount(gymAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();
    fireEvent.click(screen.getByText(/Replay v2 and ship it/));
    await screen.findByText(/Shipped/);

    expect(posted(calls, "/submit"), "the legacy submit route is unreachable from a versioned attempt").toEqual([]);
  });

  it("ships the head version through finalize, and reports what the server actually froze", async () => {
    const calls = stubApi(versioned());
    await mount(gymAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();

    fireEvent.click(screen.getByText(/Replay v2 and ship it/));
    await screen.findByText(/Shipped/);

    const [finalize] = posted(calls, "/finalize");
    expect(finalize, "shipping goes to the finalize route the backend exposes").toBeDefined();
    expect(finalize.path).toBe("/api/sessions/att-1/finalize");
    // The HEAD, not the version being read and not v1: head is the attempt's answer.
    expect(finalize.body).toEqual({ versionId: "V2", kind: "golden" });
    // Read back what the write produced, rather than what the click hoped for.
    expect(screen.getByText(/sub-77/), "the shipped sample is named from the server's response").toBeDefined();
    expect(screen.getByText(/2 steps · reward 1/)).toBeDefined();
  });

  it("will not ship a version nobody approved, and names who has to approve it", async () => {
    // finalize.py raises NotApproved for this, so an enabled button here would
    // spend a replay to deliver a refusal the screen could have explained first.
    const calls = stubApi(versioned("candidate"));
    await mount(gymAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();

    expect(screen.queryByText(/Replay v2 and ship it/)).toBeNull();
    expect(screen.getByText(/v2 is the head, but nobody has approved it/)).toBeDefined();
    expect(posted(calls, "/finalize"), "nothing is sent until it can succeed").toEqual([]);
  });

  it("will not ship before a benchmark, because running it is what saves the suite", async () => {
    // finalize refuses an attempt with no suite (409, api/versions.py:266), and
    // runBenchmark is the call that persists one.
    stubApi(versioned());
    await mount(gymAttempt);
    approveSteps();
    generateSuite();

    expect(screen.getByText(/Run the benchmark in step 2 above/)).toBeDefined();
    expect(screen.queryByText(/Replay v2 and ship it/)).toBeNull();
  });

  it("turns a replay rejection into the step an annotator can go and look at", async () => {
    // FastAPI sends this detail as an OBJECT (api/versions.py:281), and its `at`
    // counts actions from zero while every step list on screen counts from one.
    // Rendered raw it reads "[object Object]" — the one actionable message lost.
    stubApi((path) => {
      if (path.endsWith("/finalize")) {
        return { status: 422, body: { detail: { error: "the approved version does not replay cleanly", reason: "element not found", at: 1 } } };
      }
      return versioned()(path);
    });
    await mount(gymAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();
    fireEvent.click(screen.getByText(/Replay v2 and ship it/));

    await screen.findByText(/does not replay cleanly — element not found \(at step 2\)/);
    expect(screen.queryByText(/Shipped/), "a refused finalize ships nothing").toBeNull();
  });

  it("explains the version path in the same words its buttons use", async () => {
    // The guide and the fork buttons read from FORK_COPY, so an annotator cannot
    // be told one thing and shown a button that does another.
    stubApi(versioned());
    await mount(gymAttempt);

    const guide = screen.getByText(new RegExp(FORK_COPY.before.action));
    expect(guide.textContent).toContain(FORK_COPY.after.action);
    expect(guide.textContent, "rejecting a step means it is absent from the child").toContain("will not appear in the new version");
    expect(screen.getByText(/right now that is v2/), "which version is head is stated, not inferred").toBeDefined();
    expect(screen.getByText(/Finalize \(step 3 below\) replays v2/), "and so is what shipping will do to it").toBeDefined();
  });

  it("says that rounds recorded on the retired path will not ship, instead of hiding them", async () => {
    // Work already in the database cannot be migrated into a version. Saying
    // nothing would let an annotator ship believing those rounds were included.
    stubApi((path, method) => {
      if (path.endsWith("/sessions") && method === "POST") {
        return { status: 200, body: { ...SNAPSHOT, rerunFrom: 1, branch: { fromStep: 1, mode: "agent", steps: [{ idx: 2, type: "click", tabId: "tab-1", description: "re-run step" }] } } };
      }
      return versioned()(path);
    });
    await mount(gymAttempt);

    expect(screen.getByText(/correction rounds recorded on the retired path, from step 1/)).toBeDefined();
    expect(screen.getByText(/Read those rounds/), "and they stay readable — read-only is not a second path").toBeDefined();
  });
});

// --------------------------------------------------------------------------- an attempt with no version graph

describe("an attempt with no version rows", () => {
  it("renders the path it was started on, untouched", async () => {
    // Rewriting history for work already in flight is worse than two code paths,
    // so this attempt must look exactly as it did before the version path existed.
    const calls = stubApi();
    await mount(legacyAttempt);

    expect(screen.getByText(LEGACY_CORRECT), "the per-step correction is still offered").toBeDefined();
    expect(screen.queryByText(/Ship v\d+ as this attempt's sample/), "and no finalize section appears").toBeNull();
    expect(posted(calls, "/versions/baseline"), "nothing is migrated on open").toEqual([]);

    approveSteps();
    generateSuite();
    await runBenchmark();
    expect(screen.getByText(LEGACY_SUBMIT)).toBeDefined();
  });

  it("still submits through the route it always used", async () => {
    // The wiring, not the handler: this path is untouched only if the click still
    // reaches the server.
    const calls = stubApi();
    await mount(legacyAttempt);
    approveSteps();
    generateSuite();
    await runBenchmark();

    fireEvent.click(screen.getByText(LEGACY_SUBMIT));
    await screen.findByText(/Submitted to dataset/);

    const [submit] = posted(calls, "/submit");
    expect(submit.path).toBe("/api/sessions/att-1/submit");
    expect(submit.body).toMatchObject({ reward: 1, kind: "golden" });
    expect(posted(calls, "/finalize"), "and never through finalize").toEqual([]);
  });
});

// --------------------------------------------------------------------------- before the lineage is known

describe("while the attempt's lineage is still being read", () => {
  it("offers neither path, because guessing wrong is how the old submit reappears", async () => {
    // "No answer yet" and "no versions" are different facts. Treating the first
    // as the second paints the retired submit onto a versioned attempt for as
    // long as the GET takes — and that window is enough to press it.
    stubApi((path) => (path.endsWith("/versions") ? { status: 0, body: null } : undefined));
    await mount(gymAttempt, { undecided: true }); // the lineage read never comes back
    approveSteps();
    generateSuite();
    await runBenchmark();

    expect(screen.queryByText(LEGACY_SUBMIT)).toBeNull();
    expect(screen.queryByText(LEGACY_CORRECT)).toBeNull();
    expect(screen.queryByText(/Replay v\d+ and ship it/), "and nothing claims to be shippable yet").toBeNull();
    await waitFor(() => expect(screen.getByText(/Reading this attempt's version lineage/)).toBeDefined());
  });
});

describe("while the session itself is still being opened", () => {
  it("does not show the retired legacy submit on an attempt that turns out to be versioned", async () => {
    // The bug this closes: `path` read `!sessionId ? "legacy"`, and sessionId is
    // null for the WHOLE duration of the opening POST. So a versioned gym attempt
    // rendered — and armed — the retired legacy Submit until that request landed,
    // which is precisely the window the retirement exists to close. The suite
    // passed it, because every other test let the open resolve first.
    const { LineagePanel } = await import("./TaskReview");
    render(<LineagePanel sessionId={null} sessionSettled={false} isGym />);
    await waitFor(() => {
      expect(screen.queryByText(/Approve & submit to dataset/i)).toBeNull();
    });
  });

  it("settles to the legacy path only once the open has actually answered", async () => {
    const { LineagePanel } = await import("./TaskReview");
    const seen: string[] = [];
    const { rerender } = render(
      <LineagePanel sessionId={null} sessionSettled={false} isGym onLineage={(l) => seen.push(l.path)} />,
    );
    await waitFor(() => expect(seen).toContain("unknown"));
    rerender(<LineagePanel sessionId={null} sessionSettled isGym onLineage={(l) => seen.push(l.path)} />);
    await waitFor(() => expect(seen).toContain("legacy"));
    expect(seen.indexOf("unknown")).toBeLessThan(seen.indexOf("legacy"));
  });
});
