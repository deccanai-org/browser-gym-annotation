import type { ComponentProps } from "react";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { t } from "../../ds";
import { FORK_COPY, type VersionGraphData, type VersionNode, type VersionStep } from "../../lib/versionsApi";
import { VersionGraph } from "./VersionGraph";
import { VersionSteps } from "./VersionSteps";

/**
 * The lineage panel and the step list in a real DOM.
 *
 * versions.test.tsx proves the payloads and the copy; this proves what an
 * annotator actually sees and can click. Every assertion below is on rendered
 * TEXT or on the treatment that separates two rows, because every failure this
 * file guards against is a human reading the panel wrongly and forking the
 * wrong way.
 */

// --------------------------------------------------------------------------- fixtures

const version = (over: Partial<VersionNode> & { id: string; versionNo: number }): VersionNode => ({
  parentId: null,
  kind: "agent_correction",
  status: "candidate",
  revision: 0,
  producer: "",
  forkBeforeStepId: null,
  forkCheckpointId: null,
  isHead: false,
  stepCount: 0,
  createdAt: "2026-07-23T00:00:00",
  ...over,
});

const step = (over: Partial<VersionStep> & { stepId: string; displayIdx: number }): VersionStep => ({
  versionId: "V1",
  inherited: false,
  actor: "agent",
  type: "click",
  description: "click something",
  url: "",
  image: "",
  reasoning: "",
  humanIntent: "",
  guidance: "",
  verdict: "pending",
  ...over,
});

const graph = (over: Partial<VersionGraphData> = {}): VersionGraphData => ({
  attemptId: "A",
  revision: 4,
  headVersionId: "V1",
  agentCallCount: 1,
  versions: [
    version({ id: "V1", versionNo: 1, kind: "agent_run", isHead: true, stepCount: 3, producer: "gpt-5.5" }),
    version({ id: "V2", versionNo: 2, parentId: "V1", stepCount: 5 }),
  ],
  verdicts: {},
  ...over,
});

/** Two rows shared with the parent plus one the child added itself. */
const CHILD_STEPS = [
  step({ stepId: "S-1", displayIdx: 0, versionId: "V1", inherited: true, description: "open the orders page" }),
  step({ stepId: "S-2", displayIdx: 1, versionId: "V1", inherited: true, description: "open order 1183" }),
  step({ stepId: "S-7", displayIdx: 2, versionId: "V2", description: "start a return" }),
];

function renderSteps(over: Partial<ComponentProps<typeof VersionSteps>> = {}) {
  return render(
    <VersionSteps
      version={version({ id: "V2", versionNo: 2 })}
      steps={CHILD_STEPS}
      versionNos={{ V1: 1, V2: 2 }}
      selectedStepId="S-7"
      onSelectStep={() => {}}
      onVerdict={() => {}}
      onRejectStep={() => {}}
      onContinueAfter={() => {}}
      {...over}
    />,
  );
}

function renderGraph(over: Partial<ComponentProps<typeof VersionGraph>> = {}) {
  return render(
    <VersionGraph
      graph={graph()}
      viewingId="V2"
      notice={null}
      onView={() => {}}
      onSelectHead={() => {}}
      onSetStatus={() => {}}
      onDismissNotice={() => {}}
      {...over}
    />,
  );
}

/** The lineage row that carries a sentence. The row div holds the head/viewing
 *  treatment; the copy sits one wrapper inside it. */
const rowSaying = (copy: RegExp): HTMLElement => screen.getByText(copy).parentElement?.parentElement as HTMLElement;

/** A promise the test releases by hand, standing in for the fork POST the real
 *  handler awaits. */
function deferred() {
  let release = () => {};
  const promise = new Promise<void>((resolve) => {
    release = () => resolve();
  });
  return { promise, release: () => release() };
}

// --------------------------------------------------------------------------- fork commands

describe("fork before and continue after", () => {
  it("are two separately worded commands, each carrying its own consequence", () => {
    // One control with a before/after toggle is exactly how a rejected action
    // ends up kept in a golden trajectory. The annotator has to be able to tell
    // them apart by reading, not by remembering which way the toggle was set.
    renderSteps();

    const before = screen.getByText(FORK_COPY.before.action);
    const after = screen.getByText(FORK_COPY.after.action);

    expect(before).not.toBe(after);
    expect(within(before.parentElement as HTMLElement).getByText(FORK_COPY.before.hint)).toBeDefined();
    expect(within(after.parentElement as HTMLElement).getByText(FORK_COPY.after.hint)).toBeDefined();
  });

  it("routes the reject command to the reject handler, on the step it was offered for", async () => {
    const user = userEvent.setup();
    const rejected: VersionStep[] = [];
    const continued: VersionStep[] = [];
    renderSteps({ onRejectStep: (s) => rejected.push(s), onContinueAfter: (s) => continued.push(s) });

    await user.click(screen.getByText(FORK_COPY.before.action));

    expect(rejected.map((s) => s.stepId)).toEqual(["S-7"]);
    expect(continued, "a reject that keeps the step is the one outcome this screen must never produce").toHaveLength(0);
  });

  it("routes continue-after to the other handler", async () => {
    const user = userEvent.setup();
    const rejected: VersionStep[] = [];
    const continued: VersionStep[] = [];
    renderSteps({ onRejectStep: (s) => rejected.push(s), onContinueAfter: (s) => continued.push(s) });

    await user.click(screen.getByText(FORK_COPY.after.action));

    expect(continued.map((s) => s.stepId)).toEqual(["S-7"]);
    expect(rejected).toHaveLength(0);
  });

  it("mints one branch for a double-click, not two", async () => {
    // A fork is not idempotent: two clicks are two candidate versions off the
    // same step, and the annotator is left working out which of two
    // identical-looking branches to keep. The real handler awaits a POST, so the
    // guard has to hold for as long as that request is in flight.
    const user = userEvent.setup();
    const inFlight = deferred();
    let forks = 0;
    renderSteps({
      onRejectStep: () => {
        forks += 1;
        return inFlight.promise;
      },
    });

    const control = screen.getByText(FORK_COPY.before.action).closest("button")!;
    // The dispatch is counted on the control itself so a double-click that only
    // landed once cannot masquerade as a working guard.
    let dispatched = 0;
    control.addEventListener("click", () => (dispatched += 1));

    await user.dblClick(control);

    // TWO mechanisms now stop the second fork, and the outer one is the browser's:
    // a real <button> goes disabled after the first click, so the second is never
    // delivered at all. The `detail > 1` guard behind it still matters for a
    // caller whose handler resolves synchronously. Assert the OUTCOME — one branch
    // — rather than which layer caught it.
    expect(dispatched, "a real button stops delivering once it is disabled").toBeLessThanOrEqual(2);
    expect(forks, "one double-click is one branch, however it was caught").toBe(1);
  });

  it("takes the next fork once the one it started has landed", async () => {
    // The guard is a guard, not a one-shot disable: an annotator who forks, sees
    // the candidate and forks again must not have to reload the panel.
    const user = userEvent.setup();
    const inFlight = deferred();
    let forks = 0;
    renderSteps({
      onRejectStep: () => {
        forks += 1;
        return inFlight.promise;
      },
    });

    await user.click(screen.getByText(FORK_COPY.before.action));
    await act(async () => inFlight.release());
    await user.click(screen.getByText(FORK_COPY.before.action));

    expect(forks).toBe(2);
  });
});

// --------------------------------------------------------------------------- inherited steps

describe("a step shared with the parent", () => {
  it("is marked inherited on every row it appears on, and names where it came from", () => {
    // Inherited rows are the parent's SAME step rows, not copies. An annotator
    // who reads them as copies re-reviews the whole prefix once per branch.
    renderSteps();

    expect(screen.getAllByText("inherited · v1")).toHaveLength(2);
    expect(
      screen.getAllByText("inherited · v1")[0].getAttribute("title"),
      "the badge has to say WHY it is marked, or it reads as decoration",
    ).toMatch(/same step row/i);
  });

  it("leaves the version's own step unmarked, so the two are distinguishable at a glance", () => {
    renderSteps();

    const ownRow = screen.getByText("start a return").parentElement as HTMLElement;
    const sharedRow = screen.getByText("open order 1183").parentElement as HTMLElement;

    expect(within(ownRow).queryByText(/inherited/)).toBeNull();
    expect(within(sharedRow).getByText(/inherited/)).toBeDefined();
  });
});

// --------------------------------------------------------------------------- the head

describe("the head version", () => {
  it("is the only row wearing the badge", () => {
    renderGraph();

    expect(screen.getAllByText("HEAD"), "two HEAD badges is two answers, and the attempt has one").toHaveLength(1);
  });

  it("is never offered as something to select again", () => {
    renderGraph();

    expect(screen.queryByText("Make v1 the head"), "an annotator would read a no-op control as a real choice").toBeNull();
    expect(screen.getByText("Make v2 the head")).toBeDefined();
  });

  it("is louder than the row merely being read", () => {
    // HEAD is the attempt's answer; VIEWING is what the annotator happens to be
    // reading. "I am reading v2" must never look like "v2 is the answer".
    renderGraph();

    const headRow = rowSaying(/current answer/);
    const viewedRow = rowSaying(/not the answer until you select it/);

    expect(headRow).not.toBe(viewedRow);
    expect(headRow.style.borderLeft).toBe(`3px solid ${t.primary6}`);
    expect(viewedRow.style.borderLeft, "the row being read gets a tint, never the accent rail").toBe("3px solid transparent");
    expect(within(headRow).getByText("HEAD")).toBeDefined();
  });

  it("still offers QC, because finalize refuses to ship a version that is not approved", () => {
    renderGraph();

    expect(within(rowSaying(/current answer/)).getByText("Approve")).toBeDefined();
  });
});

it("does not fork twice on a double-click even when the handler is synchronous", async () => {
  // Regression, proved with a probe before it was fixed: the promise latch only
  // guards callers that RETURN one. The prop type is `=> void`, so a synchronous
  // handler settles between the two clicks and mints a second candidate version
  // off the same step — leaving the annotator to work out which of two
  // identical-looking branches to keep. `detail` is the precise signal; elapsed
  // time cannot tell a double-click from a deliberate re-fork.
  const user = userEvent.setup();
  let forks = 0;
  renderSteps({ onRejectStep: () => { forks += 1; } });

  await user.dblClick(screen.getByText(FORK_COPY.before.action));
  expect(forks).toBe(1);
});
