import { describe, expect, it } from "vitest";
import { refusalText } from "./api";

/**
 * What the annotator is told when a ship is refused.
 *
 * The finalize gate is fail-closed: a check that could not be RUN blocks a ship
 * exactly as loudly as one that ran and said no. Those have opposite fixes — redo
 * the task versus fix the check — and the refusal used to be a single sentence
 * that could not tell them apart, so the annotator's only move was to redo a task
 * that may have been fine.
 */

function res(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("a refused ship", () => {
  it("names the check that could not be run", async () => {
    const text = await refusalText(res(409, {
      detail: {
        error: "this version cannot be scored: is_polite could not be run. Fix or remove "
             + "those checks — a sample is only worth what its verifiers actually proved.",
        results: { is_polite: "unknown" },
        executed: [],
        unproven: ["is_polite"],
      },
    }));
    expect(text).toContain("is_polite");
    expect(text).toContain("could not be run");
  });

  it("names the check that failed, which is a different problem", async () => {
    const text = await refusalText(res(409, {
      detail: {
        error: "this version does not pass cart_empty. Fix the run, or ship it deliberately "
             + "as a breaker if that is what you mean.",
        results: { cart_empty: "fail" },
        executed: ["cart_empty"],
        unproven: [],
      },
    }));
    expect(text).toContain("cart_empty");
    expect(text).not.toContain("could not be run");
  });

  it("still handles a plain-string refusal", async () => {
    // Most refusals in the app are a bare string; structuring one must not break
    // the rest.
    const text = await refusalText(res(409, { detail: "that attempt is already submitted" }));
    expect(text).toBe("that attempt is already submitted");
  });

  it("falls back to the status when the body is not JSON", async () => {
    const text = await refusalText(new Response("gateway timeout", { status: 504 }));
    expect(text).toContain("504");
  });
});
