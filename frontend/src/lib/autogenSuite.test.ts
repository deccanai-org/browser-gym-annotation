/** Reading a cached generated suite.
 *
 *  "None yet" and "an empty one" are different answers — the screen offers
 *  different things for each (generate one, or use the one that exists) — so a
 *  response that carries no checks must not be reported as a suite.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchCachedAutogenSuite } from "./api";

const reply = (status: number, body: unknown) =>
  vi.stubGlobal("fetch", async () => ({ ok: status < 400, status, json: async () => body }) as Response);

afterEach(() => vi.unstubAllGlobals());

describe("the cached generated suite", () => {
  it("is returned when one exists", async () => {
    reply(200, { taskId: "M1/x", seed: 0, oracle: true, brief: "b", iterations: 2,
                 checks: [{ id: "a", level: "backend", assertion: "x", code: "c", check: {} }] });
    const out = await fetchCachedAutogenSuite("M1/x");
    expect(out?.oracle).toBe(true);
    expect(out?.checks).toHaveLength(1);
  });

  it("is null when the task has none — not an error", async () => {
    reply(404, { detail: "no generated suite cached for this task yet" });
    expect(await fetchCachedAutogenSuite("M1/x")).toBeNull();
  });

  it("is null when the body carries no checks, whatever the status", async () => {
    // Guards the offer on the review screen: it renders a count, so a
    // shape-shaped-but-empty response would have thrown mid-render.
    reply(200, { taskId: "M1/x", seed: 0, oracle: true });
    expect(await fetchCachedAutogenSuite("M1/x")).toBeNull();
  });

  it("is null when the server is unreachable", async () => {
    vi.stubGlobal("fetch", async () => { throw new Error("offline"); });
    expect(await fetchCachedAutogenSuite("M1/x")).toBeNull();
  });
});
