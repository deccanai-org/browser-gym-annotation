/** Who can reach QA at all.
 *
 *  The server refuses a non-reviewer, but an entry point that exists and then
 *  403s is a worse experience than one that is simply not there — and it leaks
 *  that a review surface exists to people who cannot use it.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../auth/AuthContext";
import { MyTasks } from "../my-tasks/MyTasks";

const BOARD = {
  annotator: { name: "Ana", email: "ana@deccan.ai", role: "reviewer" },
  batch: "sellable-breakers-v2",
  assigned: 1,
  quota: { submitted: 1, accepted: 0, target: 1 },
  counts: { todo: 0, in_progress: 0, returned: 1, in_review: 0, submitted: 0, all: 1 },
  nextUp: null,
  tasks: [{
    id: "M1/x", title: "A task", category: "M", difficulty: "hard", prompt: "p",
    primaryApp: "shop", sites: [], status: "returned", resumeStep: null,
    sessionId: "s1", reworkNote: "the cart was left with an extra item", updatedAt: null,
  }],
};

function stub() {
  // Both the board and /auth/me answer from here; the board shape is a superset
  // of what the auth context reads.
  vi.stubGlobal("fetch", async (url: string) =>
    ({ ok: true, status: 200, json: async () => (String(url).includes("/auth/me") ? BOARD.annotator : BOARD) }) as Response);
}

const mount = (props: Parameters<typeof MyTasks>[0]) =>
  render(<AuthProvider><MyTasks {...props} /></AuthProvider>);

afterEach(() => vi.unstubAllGlobals());

describe("the QA entry point", () => {
  it("is not rendered for someone who cannot use it", async () => {
    stub();
    mount({ onOpenTask: () => {} });                    // no onOpenQa = not a reviewer
    expect(await screen.findByText(/curated breakers/)).toBeTruthy();   // board is up
    expect(screen.queryByText(/QA review/)).toBeNull();
  });

  it("is rendered for a reviewer", async () => {
    stub();
    mount({ onOpenTask: () => {}, onOpenQa: () => {} });
    expect(await screen.findByText(/QA review/)).toBeTruthy();
  });
});

describe("a returned task", () => {
  it("shows the reviewer's reason, so it is actionable", async () => {
    // "Returned" on its own tells someone to redo the work without saying what
    // was wrong.
    stub();
    mount({ onOpenTask: () => {} });
    // the board opens on "To do"; returned work lives on its own tab
    fireEvent.click(await screen.findByText("Returned"));
    expect(await screen.findByText(/extra item/)).toBeTruthy();
  });
});
