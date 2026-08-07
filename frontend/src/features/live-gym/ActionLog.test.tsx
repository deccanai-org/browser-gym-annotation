/** The state-change line — what a step did to the WORLD.
 *
 *  The three states are the whole point: not observed, observed-and-nothing-
 *  moved, and moved. Collapsing the first two would tell an annotator their
 *  action was a no-op when in truth we simply did not look.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActionLog, type LoggedStep } from "./ActionLog";

const base: LoggedStep = {
  stepId: "s1", index: 0, actionType: "click", description: "click Buy now",
  replayState: "unverified", tabId: "shop",
};

const renderLog = (steps: LoggedStep[]) =>
  render(<ActionLog steps={steps} />);

describe("the state change under a step", () => {
  it("says nothing when the step was never observed", () => {
    // It shared an observation window with a later step. Silence is the honest
    // rendering; "no state change" here would be a claim we cannot make.
    renderLog([{ ...base, worldDelta: undefined }]);
    expect(screen.queryByText(/no state change/i)).toBeNull();
  });

  it("reports a click that changed nothing", () => {
    renderLog([{ ...base, worldDelta: { changed: false } }]);
    expect(screen.getByText(/no state change/i)).toBeTruthy();
  });

  it("names the change and the app it happened in", () => {
    renderLog([{
      ...base,
      worldDelta: { changed: true, apps: ["shop"], summary: "orders +ORD_7" },
      stateChange: "orders +ORD_7",
    }]);
    expect(screen.getByText("orders +ORD_7")).toBeTruthy();
    expect(screen.getByText("shop")).toBeTruthy();
  });

  it("names every app a cross-app action touched", () => {
    // Placing an order also lands a confirmation mail. Seeing both is the
    // signal these multi-app tasks exist to test.
    renderLog([{
      ...base,
      worldDelta: { changed: true, apps: ["mail", "shop"], summary: "orders +ORD_7; unread_count 0 → 1" },
      stateChange: "orders +ORD_7; unread_count 0 → 1",
    }]);
    expect(screen.getByText("shop")).toBeTruthy();
    expect(screen.getByText("mail")).toBeTruthy();
  });
});


describe("the dropped-interactions alert", () => {
  it("is silent while nothing has been lost", () => {
    renderLog([base]);
    expect(screen.queryByText(/lost|dropped/i)).toBeNull();
  });

  it("says so when the recorder had to drop interactions", () => {
    // From that point the trajectory is incomplete, and only the annotator can
    // decide whether to redo the task. The alert existed but nothing passed the
    // count, so it could never fire.
    render(<ActionLog steps={[base]} dropped={3} />);
    expect(screen.getByText(/3/)).toBeTruthy();
  });
});


describe("the collapse-to-rail", () => {
  it("folds to a rail that keeps the step count and the live signal", () => {
    render(<ActionLog steps={[base, { ...base, stepId: "s2", index: 1 }]} />);
    fireEvent.click(screen.getByTitle(/fold the trajectory away/i));
    // The rail keeps the count so the annotator still knows recording is alive.
    const rail = screen.getByLabelText("Recorded actions");
    expect(rail.textContent).toContain("2");
    // ...and it is reversible.
    fireEvent.click(rail);
    expect(screen.getByText(/recording as you work/i)).toBeTruthy();
  });

  it("turns the rail RED and shows the count when interactions were dropped", () => {
    // The regression this guards: a first cut of the rail hid the dropped-
    // interactions alert behind an unconditional green dot, so an annotator who
    // folded the trajectory would never learn the recording was incomplete.
    render(<ActionLog steps={[base]} dropped={2} />);
    fireEvent.click(screen.getByTitle(/fold the trajectory away/i));
    const rail = screen.getByLabelText("Recorded actions");
    expect(rail.textContent, "the fold must still surface the loss").toMatch(/2/);
    expect(rail.textContent?.toLowerCase()).toContain("lost");
    expect(rail.getAttribute("title")?.toLowerCase()).toContain("lost");
  });
})
