/** The state-change line — what a step did to the WORLD.
 *
 *  The three states are the whole point: not observed, observed-and-nothing-
 *  moved, and moved. Collapsing the first two would tell an annotator their
 *  action was a no-op when in truth we simply did not look.
 */
import { render, screen } from "@testing-library/react";
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
