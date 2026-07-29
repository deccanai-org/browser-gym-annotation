import { afterEach, describe, expect, it, vi } from "vitest";
import { attachLiveBrowser, closeLiveBrowser, currentLiveBrowser } from "./liveSessionApi";

interface Call {
  url: string;
  method: string;
  body: Record<string, unknown> | null;
  keepalive: boolean;
}

/** Records every request and answers from `reply`, so a test can assert what the
 *  client did NOT send as easily as what it did. */
function stubFetch(reply: (call: Call) => { status: number; body?: unknown }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const call: Call = {
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null,
      keepalive: init?.keepalive === true,
    };
    calls.push(call);
    const { status, body } = reply(call);
    return { ok: status >= 200 && status < 300, status, json: async () => body ?? {} } as Response;
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

const opened = (ticket: string) => ({
  sessionId: "live-7",
  ticket,
  viewport: { width: 1280, height: 800 },
  url: "http://127.0.0.1:9411/",
});

describe("opening the attempt's live browser", () => {
  it("sends nothing, so the server chooses both the URL and the ticket owner", async () => {
    // A client-supplied owner is how you get a ticket the live service answers
    // with close 4401 — a socket that never streams and never errors — and a
    // client-supplied URL would bypass the attempt's isolated workspace.
    const calls = stubFetch(() => ({ status: 200, body: opened("tkt-1") }));
    const res = await attachLiveBrowser("att-1");

    expect(res.ok && res.value.ticket).toBe("tkt-1");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/sessions/att-1/live");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body, "the body carries no owner and no url").toEqual({});
  });

  it("reads back the viewport the server opened, not a client default", async () => {
    // The pane maps every click to a fraction of THIS viewport; guessing it
    // mis-places every input by the ratio and nothing reports a failure.
    stubFetch(() => ({ status: 200, body: { ...opened("tkt-1"), viewport: { width: 1024, height: 768 } } }));
    const res = await attachLiveBrowser("att-1");
    expect(res.ok && res.value.viewport).toEqual({ width: 1024, height: 768 });
  });

  it("surfaces a 409 as the live browser service being unreachable", async () => {
    // The annotator can start that service. Collapsing this into a generic
    // failure sends them looking for a bug in the pane instead.
    stubFetch(() => ({ status: 409, body: { detail: "the live browser service is unreachable at http://localhost:8877" } }));
    const res = await attachLiveBrowser("att-1");

    expect(res.ok).toBe(false);
    if (res.ok) return;
    expect(res.kind).toBe("unavailable");
    expect(res.message).toContain("live browser service is unreachable");
  });

  it("reports someone else's attempt as missing, never as forbidden", async () => {
    // The backend answers 404 rather than 403 so that probing ids cannot
    // enumerate other annotators' attempts; the client must not re-label it.
    stubFetch(() => ({ status: 404, body: { detail: "session not found" } }));
    const res = await attachLiveBrowser("att-someone-else");
    expect(res.ok === false && res.kind).toBe("missing");
  });

  it("distinguishes a dead network from a server that answered", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    const res = await attachLiveBrowser("att-1");
    expect(res.ok === false && res.kind).toBe("offline");
  });
});

describe("re-attaching after a reload", () => {
  it("finds the browser the previous mount left open without opening another", async () => {
    // The unmount cleanup never runs on a reload, so this probe is the only
    // thing standing between one attempt and a second Chromium per refresh.
    const calls = stubFetch(() => ({ status: 200, body: { session: opened("tkt-stale") } }));
    const res = await currentLiveBrowser("att-1");

    expect(res.ok && res.value?.sessionId).toBe("live-7");
    expect(calls[0].method, "a probe must not have the side effect it is checking for").toBe("GET");
  });

  it("reports nothing open as null rather than as a failure", async () => {
    stubFetch(() => ({ status: 200, body: { session: null } }));
    const res = await currentLiveBrowser("att-1");
    expect(res.ok && res.value).toBeNull();
  });

  it("mints a fresh ticket for the browser that is already open", async () => {
    // Tickets expire (LIVE_TICKET_TTL_S). Connecting with the one the probe
    // returned closes the socket 4401; re-opening instead would strand a live
    // browser and start a second. So the same session comes back re-ticketed.
    const calls = stubFetch((c) =>
      c.method === "GET" ? { status: 200, body: { session: opened("tkt-stale") } } : { status: 200, body: opened("tkt-fresh") },
    );
    const found = await currentLiveBrowser("att-1");
    const attached = await attachLiveBrowser("att-1");

    expect(found.ok && found.value?.sessionId).toBe("live-7");
    expect(attached.ok && attached.value.sessionId, "the same browser, not a new one").toBe("live-7");
    expect(attached.ok && attached.value.ticket).toBe("tkt-fresh");
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });
});

describe("closing the attempt's live browser", () => {
  it("survives the navigation that triggered it", async () => {
    // This is sent from an unmount. Without keepalive the browser cancels it as
    // the view goes away, and a Chromium is left running per task visited.
    const calls = stubFetch(() => ({ status: 200, body: { closed: true } }));
    const res = await closeLiveBrowser("att-1");

    expect(res.ok && res.value.closed).toBe(true);
    expect(calls[0].url).toBe("/api/sessions/att-1/live/close");
    expect(calls[0].keepalive, "an unmount request the browser cancels leaks the browser").toBe(true);
  });

  it("escapes the attempt id into the path", async () => {
    const calls = stubFetch(() => ({ status: 200, body: { closed: true } }));
    await closeLiveBrowser("att 1/../2");
    expect(calls[0].url).toBe("/api/sessions/att%201%2F..%2F2/live/close");
  });
});
