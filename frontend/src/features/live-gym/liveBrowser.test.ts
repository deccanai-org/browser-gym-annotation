import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EVENT_QUEUE_CAP,
  EventRecorder,
  LiveSocket,
  backoffMs,
  describeAt,
  describeFocused,
  normalizePoint,
  openLiveSession,
  scaleDelta,
  streamUrl,
} from "../../lib/liveBrowser";
import type { LiveSocketConfig, LiveState, Timers } from "../../lib/liveBrowser";

// --------------------------------------------------------------------------- doubles

/** A websocket the test drives by hand. Cast to WebSocket at the injection point
 *  so production code keeps the real type and only the fake is fabricated. */
class FakeSocket {
  sent: string[] = [];
  closed = false;
  /** OPEN. Mirrors the real thing because a socket in CLOSING/CLOSED accepts
   *  send() and silently discards the payload — the exact behaviour that made
   *  input into a dying socket look delivered. */
  readyState = 1;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  send(raw: string): void {
    if (this.closed) throw new Error("socket is closed");
    this.sent.push(raw);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  /** server → client */
  emit(msg: unknown): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  /** the socket dies underneath us (1006 = abnormal, i.e. not a service refusal) */
  drop(code = 1006): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({ code });
  }

  get messages(): Record<string, unknown>[] {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

const asWebSocket = (f: FakeSocket) => f as unknown as WebSocket;

function fakeTimers() {
  const queue = new Map<number, { fn: () => void; at: number }>();
  let handle = 0;
  let clock = 0;
  const timers: Timers = {
    set: (fn, ms) => {
      handle += 1;
      queue.set(handle, { fn, at: clock + ms });
      return handle as unknown as ReturnType<typeof setTimeout>;
    },
    clear: (h) => {
      queue.delete(h as unknown as number);
    },
  };
  return {
    timers,
    advance(ms: number) {
      clock += ms;
      // Snapshot first: a fired callback reschedules itself (that IS the backoff
      // loop), and iterating the live map would run the next attempt inside this
      // same tick and collapse the whole schedule.
      for (const [h, entry] of Array.from(queue.entries())) {
        if (entry.at <= clock) {
          queue.delete(h);
          entry.fn();
        }
      }
    },
    get pending() {
      return queue.size;
    },
  };
}

function harness(cfg: Partial<LiveSocketConfig> = {}) {
  const sockets: FakeSocket[] = [];
  const states: LiveState[] = [];
  const frames: { seq: number; data: string }[] = [];
  const clock = fakeTimers();
  const socket = new LiveSocket({
    sessionId: "abc123def456",
    ticket: "9999999999.deadbeef.b3Q",
    base: "http://live.test",
    timers: clock.timers,
    pingMs: 0,
    socketFactory: () => {
      const f = new FakeSocket();
      sockets.push(f);
      return asWebSocket(f);
    },
    onState: (s) => states.push(s),
    onFrame: (f) => frames.push(f),
    ...cfg,
  });
  const live = (controller = true) => {
    socket.connect();
    sockets[sockets.length - 1].emit({
      type: "hello",
      sessionId: "abc123def456",
      controller,
      viewport: { width: 1280, height: 800 },
    });
  };
  return { socket, sockets, states, frames, clock, live, last: () => sockets[sockets.length - 1] };
}

function fakeFetch(reply: (body: unknown, url: string) => { ok: boolean; json?: unknown } = () => ({ ok: true })) {
  const calls: { url: string; body: unknown }[] = [];
  const impl = (async (url: string, init?: RequestInit) => {
    const body = init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown);
    calls.push({ url: String(url), body });
    const r = reply(body, String(url));
    return {
      ok: r.ok,
      status: r.ok ? 200 : 503,
      json: async () => r.json ?? { recorded: Array.isArray(body) ? body.length : 0 },
    };
  }) as unknown as typeof fetch;
  return { impl, calls };
}

// --------------------------------------------------------------------------- geometry

describe("normalized coordinates", () => {
  it("maps the same on-screen point to the same fraction at every rendered size", () => {
    // The single correctness property of the whole pane: the surface is scaled to
    // whatever space it gets, the remote viewport is not, and only a fraction
    // survives the difference. Pixels would mis-place every click by the scale
    // factor, silently.
    const sizes = [
      { width: 900, height: 563 },
      { width: 1920, height: 1200 },
      { width: 1280, height: 800 },
    ];
    const points = sizes.map(({ width, height }) =>
      normalizePoint(width * 0.25, height * 0.6, { left: 0, top: 0, width, height }),
    );
    for (const p of points) {
      expect(p.nx).toBeCloseTo(0.25, 6);
      expect(p.ny).toBeCloseTo(0.6, 6);
    }
  });

  it("subtracts the surface offset so a scrolled-down pane still hits the right element", () => {
    const p = normalizePoint(340, 260, { left: 140, top: 60, width: 400, height: 400 });
    expect(p).toEqual({ nx: 0.5, ny: 0.5 });
  });

  it("clamps a point outside the surface to the viewport edge", () => {
    const rect = { left: 0, top: 0, width: 800, height: 500 };
    expect(normalizePoint(-40, 900, rect)).toEqual({ nx: 0, ny: 1 });
  });

  it("returns the origin for a zero-sized surface instead of NaN", () => {
    // NaN JSON-encodes to null and CDP places the click at the page origin — a
    // wrong click that reports success. Clamping keeps the failure visible.
    const p = normalizePoint(120, 90, { left: 0, top: 0, width: 0, height: 0 });
    expect(Number.isNaN(p.nx)).toBe(false);
    expect(p).toEqual({ nx: 0, ny: 0 });
  });
});

describe("scroll deltas", () => {
  it("re-expresses a wheel tick in page pixels", () => {
    // Points are fractions, deltas are pixels — a wheel measured on a 400px-tall
    // surface must be scaled or the page moves by a different amount than the
    // gesture the annotator made.
    expect(scaleDelta(100, 400, 800)).toBe(200);
    expect(scaleDelta(100, 800, 800)).toBe(100);
  });

  it("yields zero rather than Infinity when the surface has not been measured", () => {
    expect(scaleDelta(100, 0, 800)).toBe(0);
  });
});

// --------------------------------------------------------------------------- urls

describe("streamUrl", () => {
  it("upgrades an https base to wss so a secure page can open the stream", () => {
    const url = streamUrl("https://live.example.com", "abc", { ticket: "1.2.3" });
    expect(url.startsWith("wss://live.example.com/live/stream/abc?")).toBe(true);
  });

  it("query-encodes the ticket", () => {
    const url = streamUrl("http://live.test", "abc", { ticket: "17.sig.a+b/c=" });
    expect(url).toContain("ticket=17.sig.a%2Bb%2Fc%3D");
  });

  it("resolves a relative base against the page origin, for a same-origin proxy", () => {
    const url = streamUrl("/live", "abc", { ticket: "t", origin: "https://annotate.example.com" });
    expect(url).toBe("wss://annotate.example.com/live/live/stream/abc?ticket=t&control=true");
  });

  it("asks for read-only explicitly when control is not wanted", () => {
    expect(streamUrl("http://live.test", "abc", { ticket: "t", control: false })).toContain("control=false");
  });
});

describe("backoffMs", () => {
  it("doubles per attempt and caps, so a dead service is retried forever but slowly", () => {
    expect(backoffMs(1)).toBe(300);
    expect(backoffMs(2)).toBe(600);
    expect(backoffMs(4)).toBe(2400);
    expect(backoffMs(20)).toBe(10_000);
  });
});

// --------------------------------------------------------------------------- socket

describe("LiveSocket", () => {
  it("goes live on hello and reports whether this connection holds control", () => {
    const h = harness();
    h.live(true);
    expect(h.socket.snapshot.status).toBe("live");
    expect(h.socket.snapshot.controller).toBe(true);
    expect(h.socket.snapshot.viewport).toEqual({ width: 1280, height: 800 });
  });

  it("renders latest-wins and ignores a frame that arrives out of order", () => {
    const h = harness();
    h.live();
    h.last().emit({ type: "frame", seq: 7, data: "AAA" });
    h.last().emit({ type: "frame", seq: 9, data: "BBB" });
    h.last().emit({ type: "frame", seq: 8, data: "STALE" });
    expect(h.frames.map((f) => f.data)).toEqual(["AAA", "BBB"]);
    expect(h.socket.snapshot.frameSeq).toBe(9);
  });

  it("numbers inputs monotonically from one", () => {
    const h = harness();
    h.live();
    h.socket.click({ nx: 0.5, ny: 0.5 });
    h.socket.typeText("hi");
    expect(h.last().messages.map((m) => m.id)).toEqual([1, 2]);
  });

  it("keeps counting input ids across a reconnect instead of restarting at one", () => {
    // The service keeps last_input_id on the SESSION, not the connection: a
    // client that restarts its counter has every input answered
    // applied:false/"stale" — input that looks delivered and is not.
    const h = harness();
    h.live();
    h.socket.click({ nx: 0.1, ny: 0.1 });
    h.socket.click({ nx: 0.2, ny: 0.2 });
    h.last().drop();
    h.clock.advance(backoffMs(1));
    h.last().emit({ type: "hello", controller: true, viewport: { width: 1280, height: 800 } });
    h.socket.click({ nx: 0.3, ny: 0.3 });
    expect(h.sockets).toHaveLength(2);
    expect(h.last().messages.map((m) => m.id)).toEqual([3]);
  });

  it("refuses input while the socket is down instead of swallowing it", () => {
    const h = harness();
    const sent = h.socket.click({ nx: 0.5, ny: 0.5 });
    expect(sent).toBe(false);
    expect(h.socket.snapshot.droppedInputs).toBe(1);
    expect(h.socket.snapshot.detail).toContain("NOT delivered");
  });

  it("refuses a viewer's input locally rather than burning an input id on it", () => {
    const h = harness();
    h.live(false);
    expect(h.socket.click({ nx: 0.5, ny: 0.5 })).toBe(false);
    expect(h.last().messages).toHaveLength(0);
    expect(h.socket.snapshot.lastInputId).toBe(0);
    expect(h.socket.snapshot.detail).toContain("read-only");
  });

  it("surfaces the service's reason when an input is acked as not applied", () => {
    const h = harness();
    h.live();
    h.socket.click({ nx: 0.5, ny: 0.5 });
    h.last().emit({ type: "ack", id: 1, applied: false, reason: "stale" });
    expect(h.socket.snapshot.pendingInputs).toBe(0);
    expect(h.socket.snapshot.droppedInputs).toBe(1);
    expect(h.socket.snapshot.detail).toContain("stale");
  });

  it("clears an input from pending once it is acked", () => {
    const h = harness();
    h.live();
    h.socket.click({ nx: 0.5, ny: 0.5 });
    expect(h.socket.snapshot.pendingInputs).toBe(1);
    h.last().emit({ type: "ack", id: 1, applied: true });
    expect(h.socket.snapshot.pendingInputs).toBe(0);
    expect(h.socket.snapshot.droppedInputs).toBe(0);
  });

  it("counts inputs that were in flight when the socket died as unacked, not applied", () => {
    const h = harness();
    h.live();
    h.socket.click({ nx: 0.5, ny: 0.5 });
    h.socket.typeText("x");
    h.last().drop();
    expect(h.socket.snapshot.unackedInputs).toBe(2);
    expect(h.socket.snapshot.droppedInputs).toBe(0);
  });

  it("reconnects on the backoff schedule after the socket drops", () => {
    const h = harness();
    h.live();
    h.last().drop();
    expect(h.socket.snapshot.status).toBe("reconnecting");
    expect(h.socket.snapshot.attempt).toBe(1);
    h.clock.advance(backoffMs(1) - 1);
    expect(h.sockets).toHaveLength(1);
    h.clock.advance(1);
    expect(h.sockets).toHaveLength(2);
  });

  it("backs off further when the reconnect itself fails", () => {
    const h = harness();
    h.live();
    h.last().drop();
    h.clock.advance(backoffMs(1));
    h.last().drop();
    expect(h.socket.snapshot.attempt).toBe(2);
    h.clock.advance(backoffMs(2) - 1);
    expect(h.sockets).toHaveLength(2);
    h.clock.advance(1);
    expect(h.sockets).toHaveLength(3);
  });

  it("stops retrying on an expired ticket and says why", () => {
    // 4401/4403/4404 are decided before the handshake is accepted; retrying an
    // expired ticket is an infinite spinner that never becomes a browser.
    const h = harness();
    h.live();
    h.last().drop(4401);
    expect(h.socket.snapshot.status).toBe("closed");
    expect(h.socket.snapshot.detail).toContain("ticket rejected");
    h.clock.advance(60_000);
    expect(h.sockets).toHaveLength(1);
  });

  it("reports a refused origin rather than looking like a network blip", () => {
    const h = harness();
    h.live();
    h.last().drop(4403);
    expect(h.socket.snapshot.detail).toContain("LIVE_ALLOWED_ORIGINS");
  });

  it("does not reconnect after an intentional disconnect", () => {
    const h = harness();
    h.live();
    h.socket.disconnect();
    h.clock.advance(60_000);
    expect(h.socket.snapshot.status).toBe("closed");
    expect(h.sockets).toHaveLength(1);
  });

  it("drops control locally when the service denies an input", () => {
    const h = harness();
    h.live();
    h.last().emit({ type: "denied", reason: "read-only viewer" });
    expect(h.socket.snapshot.controller).toBe(false);
    expect(h.socket.snapshot.detail).toContain("read-only viewer");
  });

  it("refuses input dispatched into a socket that is already closing", () => {
    // A WebSocket in CLOSING/CLOSED does not throw from send(); per spec it
    // discards the payload. `status` is still "live" until the close event
    // lands, so those inputs used to look delivered, sit in `pending`, and be
    // swept into "unacked" — recording nothing and refusing nothing.
    const h = harness();
    h.live();
    h.last().readyState = 2;

    expect(h.socket.click({ nx: 0.5, ny: 0.5 })).toBe(false);

    expect(h.last().messages, "a discarded payload must not burn an input id").toHaveLength(0);
    expect(h.socket.snapshot.droppedInputs).toBe(1);
    expect(h.socket.snapshot.detail).toContain("NOT delivered");
  });

  it("reports the interactions that can never be recorded when the stream dies under them", () => {
    // The ack is the ONLY place an interaction becomes a step. An input that was
    // dispatched and never acked may still have moved the world, which leaves a
    // recorded world that moved with no step explaining why — the corruption a
    // buyer rejects. "unacked 2" in a counter bar is a number, not a warning that
    // the trajectory now has a hole.
    const lost: number[] = [];
    const h = harness({ onRecordLoss: (n) => lost.push(n) });
    h.live();
    h.socket.send({ type: "mouse", phase: "down" }, () => ({ kind: "mouseDown" }));
    h.socket.send({ type: "mouse", phase: "up" }, () => ({ kind: "mouseUp" }));

    h.last().drop();

    expect(lost).toEqual([2]);
  });

  it("does not report an interaction that was already recorded as lost", () => {
    // The guard on the above: reporting a hole that is not there would have the
    // annotator redo a task whose trajectory is whole.
    const lost: number[] = [];
    const recorded: string[] = [];
    const h = harness({ onRecordLoss: (n) => lost.push(n), onRecord: (ev) => recorded.push(ev.kind) });
    h.live();
    h.socket.send({ type: "mouse", phase: "up" }, () => ({ kind: "mouseUp" }));
    h.last().emit({ type: "ack", id: 1, applied: true, state: { url: "http://shop/x" } });

    h.last().drop();

    expect(recorded).toEqual(["mouseUp"]);
    expect(lost).toEqual([]);
  });

  it("reports a record still in flight at an intentional teardown, not just at a crash", () => {
    // The pane disconnects on unmount, and an ack in flight at that moment is as
    // lost as one killed by a network drop.
    const lost: number[] = [];
    const h = harness({ onRecordLoss: (n) => lost.push(n) });
    h.live();
    h.socket.send({ type: "mouse", phase: "up" }, () => ({ kind: "mouseUp" }));

    h.socket.disconnect();

    expect(lost).toEqual([1]);
  });

  it("sends a ping without an input id, so a viewer's keepalive is not judged stale", () => {
    const h = harness({ pingMs: 5000 });
    h.live();
    h.clock.advance(5000);
    const ping = h.last().messages.find((m) => m.type === "ping");
    expect(ping).toBeDefined();
    expect(ping?.id).toBeUndefined();
    expect(h.socket.snapshot.lastInputId).toBe(0);
  });
});

// --------------------------------------------------------------------------- recorder

describe("EventRecorder", () => {
  it("batches a burst of keystrokes into one request", () => {
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers });
    for (const ch of "wireless mouse") rec.push({ kind: "key", payload: { text: ch } });
    expect(calls).toHaveLength(0);
    clock.advance(1200);
    expect(calls).toHaveLength(1);
    expect((calls[0].body as unknown[]).length).toBe("wireless mouse".length);
  });

  it("sends a completed action immediately, without waiting out the batch timer", () => {
    // The server reads the gym world once per folded batch. An action that sits
    // in the queue for 1.2s shares its observation with whatever came next, and
    // the state change can then only be attributed to the window rather than to
    // the step. Flushing on a boundary is what makes a per-step delta real.
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers });
    rec.push({ kind: "mouseDown", payload: {} });
    expect(calls).toHaveLength(0);          // a press is not a completed action
    rec.push({ kind: "mouseUp", payload: {} });
    expect(calls).toHaveLength(1);          // the release completes it — go now
  });

  it("does not flush on every keystroke", () => {
    // The guard on the above: an edit is not finished until the field settles,
    // and a request per character is exactly what the batch exists to avoid.
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers });
    for (const ch of "hello") rec.push({ kind: "keyChar", payload: { text: ch } });
    expect(calls).toHaveLength(0);
  });

  it("sends a tab switch immediately, like any other completed action", () => {
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers });
    rec.push({ kind: "switch_tab", payload: { app: "mail" } });
    expect(calls).toHaveLength(1);
  });

  it("flushes early once the batch is full, so a long session is not held in memory", () => {
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 4 });
    for (let i = 0; i < 4; i++) rec.push({ kind: "key", payload: { text: "a" } });
    expect(calls).toHaveLength(1);
  });

  it("posts the shape the recorder endpoint expects", () => {
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 1 });
    rec.push({ kind: "mousePressed", payload: { nx: 0.5, ny: 0.25 }, target: { testId: "add-to-cart" }, url: "http://shop/x", tab: "shop" });
    expect(calls[0].url).toBe("/api/sessions/s-1/events");
    const [ev] = calls[0].body as Record<string, unknown>[];
    // clientEventId included: it is what makes a retried batch idempotent, so a
    // network drop after the server committed cannot duplicate the events.
    expect(Object.keys(ev).sort()).toEqual(["clientEventId", "kind", "payload", "tab", "target", "url"].sort());
    expect(typeof ev.clientEventId, "every event carries a distinct id").toBe("string");
    expect(ev.target).toEqual({ testId: "add-to-cart" });
    expect(ev.tab).toBe("shop");
  });

  it("stamps every event with the time the backend folds actions on", () => {
    // coalesce() pairs a press/release within 700ms and folds keystrokes within
    // 1500ms by reading payload.t. Without it every event lands at t=0 and a
    // whole session of typing folds into a single fill.
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 1, now: () => 1234 });
    rec.push({ kind: "key", payload: { text: "a" } });
    const [ev] = calls[0].body as { payload: Record<string, unknown> }[];
    expect(ev.payload.t).toBe(1234);
  });

  it("keeps a caller-supplied timestamp instead of overwriting it", () => {
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 1, now: () => 99 });
    rec.push({ kind: "mouseReleased", payload: { t: 5000 } });
    const [ev] = calls[0].body as { payload: Record<string, unknown> }[];
    expect(ev.payload.t).toBe(5000);
  });

  it("keeps a failed batch, in order, for the next flush", async () => {
    // The interaction log is append-only and ordered: a batch lost to a blip
    // corrupts every action folded after it.
    let up = false;
    const { impl, calls } = fakeFetch(() => ({ ok: up }));
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 99 });
    rec.push({ kind: "key", payload: { text: "a" } });
    rec.push({ kind: "key", payload: { text: "b" } });
    expect(await rec.flush()).toBe(0);
    expect(rec.queued).toBe(2);

    up = true;
    rec.push({ kind: "key", payload: { text: "c" } });
    expect(await rec.flush()).toBe(3);
    expect(rec.queued).toBe(0);
    const body = calls[calls.length - 1].body as { payload: Record<string, unknown> }[];
    expect(body.map((e) => e.payload.text)).toEqual(["a", "b", "c"]);
  });

  it("keeps a contiguous prefix when the backend is gone, rather than a stream with a hole", () => {
    // A mousePressed whose mouseReleased was dropped folds into a bogus `press`,
    // so the events that survive have to be a prefix, not a sample.
    const { impl } = fakeFetch(() => ({ ok: false }));
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 10_000 });
    for (let i = 0; i < EVENT_QUEUE_CAP + 5; i++) rec.push({ kind: "key", payload: { text: String(i) } });
    expect(rec.queued).toBe(EVENT_QUEUE_CAP);
    expect(rec.dropped).toBe(5);
  });

  it("is a no-op when nothing has been recorded", async () => {
    const { impl, calls } = fakeFetch();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: fakeTimers().timers });
    expect(await rec.flush()).toBe(0);
    expect(calls).toHaveLength(0);
  });
});

// --------------------------------------------------------------------------- rest

describe("live browser REST", () => {
  it("describes a point in normalized coordinates, carrying the ticket", async () => {
    const { impl, calls } = fakeFetch(() => ({ ok: true, json: { testId: "checkout", role: "button" } }));
    const target = await describeAt("sid", "tkt", { nx: 0.25, ny: 0.5 }, { base: "http://live.test", fetchImpl: impl });
    expect(calls[0].url).toBe("http://live.test/live/sessions/sid/describe");
    expect(calls[0].body).toEqual({ x: 0.25, y: 0.5, ticket: "tkt" });
    expect(target.testId).toBe("checkout");
  });

  it("yields no target when describe is unreachable, so the click still goes through", async () => {
    const { impl } = fakeFetch(() => ({ ok: false }));
    expect(await describeAt("sid", "tkt", { nx: 0, ny: 0 }, { fetchImpl: impl })).toEqual({});
  });

  it("reads the open response's snake_case session_id", async () => {
    const { impl } = fakeFetch(() => ({ ok: true, json: { session_id: "0a1b2c3d4e5f", ticket: "e.s.o" } }));
    const s = await openLiveSession("http://shop.test", "annotator@deccan.ai", { base: "http://live.test", fetchImpl: impl });
    expect(s).toEqual({ sessionId: "0a1b2c3d4e5f", ticket: "e.s.o", viewport: { width: 1280, height: 800 } });
  });

  it("returns null when the live service cannot be reached, so the pane can degrade", async () => {
    const { impl } = fakeFetch(() => ({ ok: false }));
    expect(await openLiveSession("http://shop.test", "a@b.c", { fetchImpl: impl })).toBeNull();
  });
});

// --------------------------------------------------------------------------- focus attribution
describe("describeFocused", () => {
  it("asks the REMOTE page which element has keyboard focus", async () => {
    const { impl, calls } = fakeFetch(() => ({
      ok: true,
      json: { testId: "input-password", type: "password", name: "password" },
    }));
    const out = await describeFocused("s-1", "tk", { fetchImpl: impl, base: "http://live" });
    expect(calls[0].url).toBe("http://live/live/sessions/s-1/focused");
    expect(calls[0].body).toEqual({ ticket: "tk" });
    expect(out.type).toBe("password");
  });

  it("returns an empty target rather than a stale one when the call fails", async () => {
    // The backend treats an unnamed target as SENSITIVE, so an empty result is
    // the safe answer. Inventing or reusing a target is how a password ends up
    // attributed to the email field and written in the clear.
    const { impl } = fakeFetch(() => ({ ok: false }));
    expect(await describeFocused("s-1", "tk", { fetchImpl: impl })).toEqual({});
  });

  it("is the only way to learn focus, because clicking is not how focus always moves", async () => {
    // Regression: keystrokes used to be attributed to the last POINTER target.
    // Tab, Enter-submits-and-advances and a page's own autofocus all move focus
    // with no pointer event, leaving that attribution silently wrong — and the
    // backend's redaction keys entirely off the target.
    const { impl, calls } = fakeFetch((_b, url) =>
      url.endsWith("/describe")
        ? { ok: true, json: { testId: "input-email", type: "email" } }
        : { ok: true, json: { testId: "input-password", type: "password" } },
    );
    const clicked = await describeAt("s-1", "tk", { nx: 0.1, ny: 0.1 }, { fetchImpl: impl });
    const reallyFocused = await describeFocused("s-1", "tk", { fetchImpl: impl });
    expect(clicked.type).toBe("email");
    expect(reallyFocused.type).toBe("password");
    expect(calls.map((c) => c.url.split("/").pop())).toEqual(["describe", "focused"]);
  });
});

describe("EventRecorder loss accounting", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("counts the events a partial write did not keep", async () => {
    // A 200 that recorded fewer events than it was sent is a partial write: the
    // rest are gone, and the batch cannot be replayed without duplicating what
    // did land. This used to return the server's number and let every caller
    // assume the batch had gone in whole.
    const dropped: number[] = [];
    const { impl } = fakeFetch(() => ({ ok: true, json: { recorded: 1 } }));
    const clock = fakeTimers();
    const rec = new EventRecorder({
      attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 99,
      onDrop: (n) => dropped.push(n),
    });
    for (const ch of "abc") rec.push({ kind: "keyChar", payload: { text: ch } });

    expect(await rec.flush()).toBe(1);

    expect(dropped, "two interactions vanished and the alert read zero").toEqual([2]);
  });

  it("counts what a failed final flush stranded, instead of dropping it in dispose", async () => {
    // dispose() runs when the pane is torn down. A batch that came back from a
    // failed POST at that point is held by a recorder nobody references any
    // more — those interactions are never going anywhere.
    const dropped: number[] = [];
    const { impl } = fakeFetch(() => ({ ok: false }));
    const clock = fakeTimers();
    const rec = new EventRecorder({
      attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 99,
      onDrop: (n) => dropped.push(n),
    });
    rec.push({ kind: "mouseDown", payload: {} });
    rec.push({ kind: "mouseUp", payload: {} });
    await rec.flush();
    expect(rec.queued).toBe(2);

    rec.dispose();

    expect(rec.queued).toBe(0);
    expect(dropped).toEqual([2]);
  });

  it("hands the queue to sendBeacon when the page goes away", () => {
    // A fetch started during unload is cancelled with the document, so closing
    // the tab threw away everything inside the 1.2s flush window — easily a
    // whole fill plus the click that terminated it, with nothing saying the
    // trajectory now stops mid-action.
    const beacons: { url: string; type: string }[] = [];
    vi.stubGlobal("navigator", {
      sendBeacon: (url: string, body: Blob) => {
        beacons.push({ url: String(url), type: body.type });
        return true;
      },
    });
    const { impl, calls } = fakeFetch();
    const clock = fakeTimers();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 99 });
    const listeners: Record<string, (() => void) | undefined> = {};
    const stop = rec.installUnloadFlush({
      addEventListener: (t, fn) => { listeners[t] = fn; },
      removeEventListener: (t) => { listeners[t] = undefined; },
    });
    for (const ch of "mug") rec.push({ kind: "keyChar", payload: { text: ch } });
    expect(calls, "still inside the flush window — this is exactly what used to be lost").toHaveLength(0);

    listeners.pagehide?.();

    expect(beacons).toHaveLength(1);
    expect(beacons[0].url).toBe("/api/sessions/s-1/events");
    expect(beacons[0].type, "a bare string is labelled text/plain and the endpoint parses by content type")
      .toBe("application/json");
    expect(rec.queued).toBe(0);

    stop();
    expect(listeners.pagehide, "a pane that unmounts must not keep flushing on unload").toBeUndefined();
  });

  it("also listens for beforeunload, which is the one a plain tab close fires", () => {
    vi.stubGlobal("navigator", { sendBeacon: () => true });
    const { impl } = fakeFetch();
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: fakeTimers().timers, batchAt: 99 });
    const listeners: Record<string, (() => void) | undefined> = {};
    rec.installUnloadFlush({
      addEventListener: (t, fn) => { listeners[t] = fn; },
      removeEventListener: (t) => { listeners[t] = undefined; },
    });
    rec.push({ kind: "keyChar", payload: { text: "a" } });

    listeners.beforeunload?.();

    expect(rec.queued).toBe(0);
  });

  it("counts the batch as lost when the beacon is refused, rather than assuming it went", () => {
    // sendBeacon returns false when the browser will not take the payload (over
    // quota, or no beacon support at all). Believing it is what turns a lost
    // batch into a sample that looks complete.
    const dropped: number[] = [];
    vi.stubGlobal("navigator", { sendBeacon: () => false });
    const { impl } = fakeFetch();
    const rec = new EventRecorder({
      attemptId: "s-1", fetchImpl: impl, timers: fakeTimers().timers, batchAt: 99,
      onDrop: (n) => dropped.push(n),
    });
    rec.push({ kind: "keyChar", payload: { text: "a" } });
    rec.push({ kind: "keyChar", payload: { text: "b" } });

    expect(rec.flushBeacon()).toBe(false);

    expect(dropped).toEqual([2]);
  });
});

describe("EventRecorder retry", () => {
  it("keeps trying after a failed batch instead of going quiet", async () => {
    // Regression: flush() clears the pending timer on entry and re-queues a
    // failed batch at the front — but never re-armed the timer. One failed POST
    // therefore left the batch queued with nothing scheduled to send it, and the
    // recorder went silent for the rest of the session, losing exactly the
    // interactions somebody was mid-way through recording.
    const clock = fakeTimers();
    let fail = true;
    const { impl, calls } = fakeFetch(() => ({ ok: !fail }));
    const rec = new EventRecorder({ attemptId: "s-1", fetchImpl: impl, timers: clock.timers, batchAt: 1 });

    rec.push({ kind: "click", target: { testId: "a" } });
    await Promise.resolve();
    await Promise.resolve();
    expect(calls.length).toBe(1);

    fail = false;
    clock.advance(2000);
    await Promise.resolve();
    await Promise.resolve();
    expect(calls.length).toBe(2);
    expect((calls[1].body as unknown[]).length).toBe(1);
  });
});
