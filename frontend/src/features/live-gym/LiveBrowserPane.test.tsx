import { Profiler, type ComponentProps } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OpenedSession } from "../../lib/liveBrowser";
import { LiveBrowserPane } from "./LiveBrowserPane";

/**
 * The pane in a real DOM. Everything asserted here is a rendering or event
 * behaviour that the pure-logic suite cannot reach: liveBrowser.test.ts proves
 * the socket refuses a viewer's input, this proves the annotator can SEE that it
 * did — which is the whole reason the component exists.
 */

// --------------------------------------------------------------------------- doubles

/**
 * The socket the pane opens for itself. Unlike liveBrowser.test.ts there is no
 * factory to inject — LiveBrowserPane owns its LiveSocket — so the constructor
 * is replaced globally and the test drives whichever instance the pane built.
 */
class FakeSocket {
  static opened: FakeSocket[] = [];
  sent: string[] = [];
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.opened.push(this);
  }

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {}

  /** server → client */
  receive(msg: unknown): void {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }

  drop(code: number): void {
    this.onclose?.({ code });
  }

  get messages(): Record<string, unknown>[] {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

interface Call {
  url: string;
  body: unknown;
}

/** Records every request and answers from `reply`, mirroring the fake in
 *  liveBrowser.test.ts. The pane's describe/info calls have to be answered or a
 *  pointer click never reaches the socket at all.
 *
 *  `hold` keeps a chosen call in flight. The pane dispatches a press only once
 *  describe() answers, so the gap between the two is where a fast click's
 *  ordering can go wrong — and it is unreachable with a fetch that resolves at
 *  once. */
function stubFetch(reply: (url: string) => unknown, hold?: (url: string) => Promise<void> | undefined): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), body: init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown) });
    const wait = hold?.(String(url));
    if (wait) await wait;
    return { ok: true, status: 200, json: async () => reply(String(url)) } as Response;
  });
  return calls;
}

const REPLY = (url: string): unknown =>
  url.endsWith("/describe") ? { testId: "add-to-cart", role: "button" } : { url: "http://shop.test/cart" };

const SESSION: OpenedSession = {
  sessionId: "abc123def456",
  ticket: "9999999999.deadbeef.b3Q",
  viewport: { width: 1280, height: 800 },
};

async function mountPane(
  over: Partial<ComponentProps<typeof LiveBrowserPane>> = {},
  hold?: (url: string) => Promise<void> | undefined,
  /** How the service answers. Overridden by the tests about an EMPTY describe,
   *  which is a different answer rather than a different timing. */
  reply: (url: string) => unknown = REPLY,
) {
  FakeSocket.opened = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  const calls = stubFetch(reply, hold);
  let commits = 0;

  render(
    <Profiler id="live-pane" onRender={() => (commits += 1)}>
      <LiveBrowserPane attemptId="A-1" session={SESSION} base="http://live.test" {...over} />
    </Profiler>,
  );
  // The mount effect reads the page URL; let it land so a later assertion is not
  // racing a state update from mount.
  await act(async () => {});

  const img = screen.getByAltText("live browser") as HTMLImageElement;
  const surface = img.parentElement as HTMLElement;
  const sock = () => FakeSocket.opened[FakeSocket.opened.length - 1];
  const server = async (msg: unknown) => {
    await act(async () => sock().receive(msg));
  };

  return {
    img,
    surface,
    calls,
    sock,
    server,
    commits: () => commits,
    hello: (controller: boolean) => server({ type: "hello", controller, viewport: SESSION.viewport }),
    drop: async (code: number) => {
      await act(async () => sock().drop(code));
    },
    /** jsdom ships no PointerEvent, so onPointerDown is reached with a MouseEvent
     *  of the same name — React dispatches on the event NAME and reads
     *  clientX/clientY off the native event either way. */
    pointerAt: async (clientX: number, clientY: number) => {
      await act(async () => {
        fireEvent(surface, new MouseEvent("pointerdown", { bubbles: true, clientX, clientY }));
      });
    },
    /** A COMPLETE click: press, release, and the acks that carry the state the
     *  service reports. Interactions are recorded from the ack, so a gesture
     *  without one is (correctly) never recorded. */
    clickAt: async (clientX: number, clientY: number, state?: Record<string, unknown>) => {
      await act(async () => {
        fireEvent(surface, new MouseEvent("pointerdown", { bubbles: true, clientX, clientY }));
      });
      await act(async () => {
        fireEvent(surface, new MouseEvent("pointerup", { bubbles: true, clientX, clientY }));
      });
      const sent = sock().messages.filter((m) => typeof m.id === "number");
      for (const m of sent) {
        await act(async () => server({
          type: "ack", id: m.id, applied: true,
          state: state ?? { url: "https://shop.gym.local/", tabId: "t1" },
        }));
      }
    },
  };
}

/** jsdom measures every box as zero, which would clamp every click to the page
 *  origin. The surface is the only element whose rect the pane reads. */
function sizeSurface(surface: HTMLElement, box: { left: number; top: number; width: number; height: number }): void {
  surface.getBoundingClientRect = () =>
    ({ ...box, right: box.left + box.width, bottom: box.top + box.height, x: box.left, y: box.top, toJSON: () => box }) as DOMRect;
}

afterEach(() => {
  // Unmount BEFORE the stubs go: the pane flushes its recorder on teardown, and
  // an unstubbed fetch would send a real annotator's interactions at the network.
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// --------------------------------------------------------------------------- dead stream

describe("a stream that is not live", () => {
  it("covers the whole surface rather than badging a corner of it", async () => {
    // An annotator clicking into a dead stream and seeing nothing happen is the
    // failure this component exists to prevent, so the refusal has to be in the
    // way of the click, not beside it.
    const h = await mountPane();

    const blocker = within(h.surface).getByText(/Input is not being delivered/).parentElement as HTMLElement;

    expect(h.surface.contains(blocker), "a notice outside the clickable surface is a badge").toBe(true);
    expect(blocker.style.position).toBe("absolute");
    expect(blocker.style.inset, "anything short of the full surface leaves somewhere to click into").toBe("0");
    expect(within(blocker).getByText("Connecting…"), "the blocker names the state it is blocking for").toBeDefined();
  });

  it("refuses the click it blocked, and says so where the annotator is looking", async () => {
    const h = await mountPane();
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.pointerAt(400, 300);

    expect(h.sock().messages, "input dispatched into a socket that is not live is a silent lie").toHaveLength(0);
    expect(screen.getByText("refused 1")).toBeDefined();
    expect(within(h.surface).getByText(/was NOT delivered/), "the reason belongs where the click landed").toBeDefined();
    expect(screen.getAllByText(/was NOT delivered/), "and in the counter bar, which is what an annotator scans afterwards").toHaveLength(2);
  });

  it("keeps the reconnecting spinner for a drop that may still come back", async () => {
    // The contrast that makes the terminal case below legible: a 1006 is a blip,
    // and saying so is what stops an annotator abandoning a session that is about
    // to return.
    const h = await mountPane();
    await h.hello(true);

    await h.drop(1006);

    expect(within(h.surface).getByText("Reconnecting · attempt 1")).toBeDefined();
  });
});

describe("an expired ticket", () => {
  it("is terminal: the pane stops trying and says why, instead of spinning forever", async () => {
    // 4401 is decided before the handshake is accepted. Retrying it is a spinner
    // that never becomes a browser, so the pane has to offer a decision instead.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const h = await mountPane();
    await h.hello(true);

    await h.drop(4401);

    expect(within(h.surface).getByText("Disconnected")).toBeDefined();
    expect(within(h.surface).getByText(/ticket rejected/)).toBeDefined();
    expect(screen.queryByText(/Reconnecting/), "a terminal close must not be dressed as a recoverable one").toBeNull();

    await act(async () => vi.advanceTimersByTime(60_000));
    expect(FakeSocket.opened, "a reconnect against an expired ticket can only ever fail again").toHaveLength(1);
  });

  it("still offers the annotator a way back, on the surface itself", async () => {
    const h = await mountPane();
    await h.hello(true);
    await h.drop(4401);

    await act(async () => {
      fireEvent.click(within(h.surface).getByText("Reconnect"));
    });

    expect(FakeSocket.opened, "the retry is the only exit from a terminal close").toHaveLength(2);
  });
});

// --------------------------------------------------------------------------- viewers

describe("a second annotator watching", () => {
  it("is told another connection is driving, on the surface they are trying to drive", async () => {
    const h = await mountPane({ control: false });

    await h.hello(false);

    expect(h.sock().url, "asking for control and being denied it is a different bug from never asking").toContain("control=false");
    expect(within(h.surface).getByText(/another connection is driving this browser/)).toBeDefined();
  });

  it("has its input refused rather than swallowed", async () => {
    const h = await mountPane({ control: false });
    await h.hello(false);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.pointerAt(225, 338);

    expect(h.sock().messages, "a viewer's click must not burn an input id the service would judge stale").toHaveLength(0);
    expect(screen.getByText("refused 1")).toBeDefined();
    expect(screen.getByText(/read-only viewer/)).toBeDefined();
  });
});

// --------------------------------------------------------------------------- input accounting

describe("the input counters", () => {
  it("render the service's own reason for refusing an input", async () => {
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(225, 338);

    await h.server({ type: "ack", id: 1, applied: false, reason: "stale" });

    expect(screen.getByText("refused 1")).toBeDefined();
    expect(screen.getByText("pending 0")).toBeDefined();
    expect(screen.getByText(/input 1 was not applied \(stale\)/), "a count with no reason is not actionable").toBeDefined();
  });

  it("count input that was in flight when the stream died as unacked, not as refused", async () => {
    // Refused input never left; unacked input may have applied. Only the
    // annotator can decide what to do about either, and they cannot if the pane
    // folds the two into one number.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(225, 338);
    await h.pointerAt(230, 340);

    await h.drop(1006);

    expect(screen.getByText("unacked 2")).toBeDefined();
    expect(screen.getByText("refused 0")).toBeDefined();
  });
});

// --------------------------------------------------------------------------- frames

describe("frames", () => {
  it("reach the img without re-rendering the pane", async () => {
    // At 60fps a setState per frame re-renders the whole pane, including the
    // surface the annotator is mid-gesture on. The Profiler counts commits, so a
    // frame that goes through React state fails this outright.
    const h = await mountPane();
    await h.hello(true);
    const settled = h.commits();

    await h.server({ type: "frame", seq: 1, data: "AAA" });
    await h.server({ type: "frame", seq: 2, data: "BBB" });

    expect(h.img.src).toBe("data:image/jpeg;base64,BBB");
    expect(h.commits(), "a frame must not commit a render").toBe(settled);
    expect(screen.getByAltText("live browser"), "the stream writes to one stable node, never a remounted one").toBe(h.img);

    // The counter has teeth: anything that DOES go through the pane's state
    // commits, so the assertion above is not passing on a dead profiler.
    await h.server({ type: "ack", id: 1, applied: true });
    expect(h.commits()).toBeGreaterThan(settled);
  });
});

// --------------------------------------------------------------------------- pointer geometry

describe("a click on the surface", () => {
  it("sends the same fraction for the same place on the page at two different surface sizes", async () => {
    // The surface is scaled to whatever space the pane gets; the remote viewport
    // is not. Pixels would mis-place every click by the scale factor and nothing
    // would report a failure — so this asserts the pane measures the SURFACE, not
    // the stage and not the viewport.
    const h = await mountPane();
    await h.hello(true);

    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(900 * 0.25, 563 * 0.6);

    sizeSurface(h.surface, { left: 120, top: 40, width: 1920, height: 1200 });
    await h.pointerAt(120 + 1920 * 0.25, 40 + 1200 * 0.6);

    // A press is its own message now (phase "down"), so a real release can be
    // distinguished from a synthesised one — that is what makes a drag a drag.
    await waitFor(() =>
      expect(h.sock().messages.filter((m) => m.type === "mouse" && m.phase === "down")).toHaveLength(2));
    for (const click of h.sock().messages.filter((m) => m.type === "mouse" && m.phase === "down")) {
      expect(click.nx as number).toBeCloseTo(0.25, 6);
      expect(click.ny as number).toBeCloseTo(0.6, 6);
    }
  });

  it("names the element under the pointer before dispatching, so the step is replayable", async () => {
    // A recorded pixel is not replayable, and once the click lands the element may
    // be gone — which is why describe runs first and its answer travels with the
    // recorded interaction.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.pointerAt(900 * 0.25, 563 * 0.6);

    const described = h.calls.find((c) => c.url.endsWith("/describe"));
    expect(described?.body).toEqual({ x: 0.25, y: 0.6, ticket: SESSION.ticket });
    expect(h.sock().messages.map((m) => m.type)).toEqual(["mouse"]);
  });

  it("asks a second time when the first description comes back empty", async () => {
    // `describeAt` cannot tell a point with nothing under it from a round trip
    // that failed — both answer `{}` — and the difference decides whether the
    // step can ever be replayed. Seen on a real M105 run: the click that SENT
    // the email recorded an empty locator and stranded the trajectory's last
    // step as unverified, while describing that exact point by hand answered
    // fine. The page has not moved yet at press time, so asking again is the
    // same question.
    let asked = 0;
    const dropped: number[] = [];
    const h = await mountPane({ onDropped: (n) => dropped.push(n) }, undefined, (url) => {
      if (!url.endsWith("/describe")) return { url: "http://shop.test/cart" };
      asked += 1;
      return asked === 1 ? {} : { testId: "send", role: "button" };
    });
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.clickAt(225, 338);

    expect(asked, "the empty answer must be retried").toBe(2);
    await act(async () => cleanup());
    const posted = h.calls.find((c) => c.url === "/api/sessions/A-1/events");
    const events = (posted?.body ?? []) as { kind: string; target: Record<string, string> }[];
    const down = events.find((e) => e.kind === "mouseDown");
    expect(down?.target, "the retry's answer is what gets recorded").toEqual({ testId: "send", role: "button" });
    // It answered on the retry, so nothing was lost and nothing is reported.
    expect(dropped.filter((n) => n > 0)).toHaveLength(0);
  });

  it("counts a click it could not name, instead of letting it pass for a recorded one", async () => {
    // Two empty answers is a click with no locator. It is still dispatched —
    // refusing to drive the browser would be worse — but the annotator is told
    // now rather than meeting an unshippable step at the last gate.
    const dropped: number[] = [];
    const h = await mountPane({ onDropped: (n) => dropped.push(n) }, undefined,
      (url) => (url.endsWith("/describe") ? {} : { url: "http://shop.test/cart" }));
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.clickAt(225, 338);

    await waitFor(() => expect(dropped[dropped.length - 1]).toBeGreaterThan(0));
  });

  it("offers the options instead of dispatching a click a headless browser ignores", async () => {
    // A native dropdown is browser chrome, so the headless Chromium behind the
    // screencast never paints one: measured, a click on ShopGym's Qty box moved
    // the value from 'All' to 'All'. Every task whose answer runs through a
    // <select> — a quantity, a ship-to address — was impossible to annotate.
    const h = await mountPane({}, undefined, (url) => {
      if (url.endsWith("/describe")) return { tag: "select", role: "select", name: "qty" };
      if (url.endsWith("/select-at")) {
        return { value: "1", multiple: false, options: [
          { value: "1", label: "Qty: 1", selected: true, disabled: false },
          { value: "4", label: "Qty: 4", selected: false, disabled: false }] };
      }
      return { url: "http://shop.test/product" };
    });
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await h.pointerAt(900 * 0.5, 563 * 0.5);

    // The chooser is shown...
    expect(await screen.findByRole("listbox")).toBeTruthy();
    expect(screen.getByText("Qty: 4")).toBeTruthy();
    // ...and NO mouse press was dispatched, because it would do nothing.
    expect(h.sock().messages.filter((m) => m.type === "mouse")).toHaveLength(0);
  });

  it("records the chosen option as a `select`, which the executor can replay", async () => {
    // Not as a click: `select` is in EXECUTOR_KINDS, so the step replays as the
    // same choice rather than as a click on a dropdown that never opens.
    const h = await mountPane({}, undefined, (url) => {
      if (url.endsWith("/describe")) return { tag: "select", role: "select", name: "qty" };
      if (url.endsWith("/select-at")) {
        return { value: "1", multiple: false, options: [
          { value: "4", label: "Qty: 4", selected: false, disabled: false }] };
      }
      return { url: "http://shop.test/product" };
    });
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(900 * 0.5, 563 * 0.5);

    await act(async () => {
      fireEvent(await screen.findByText("Qty: 4"),
                new MouseEvent("pointerdown", { bubbles: true }));
    });

    const sent = h.sock().messages.find((m) => m.type === "select");
    expect(sent, "the choice must reach the remote browser").toBeTruthy();
    expect(sent?.value).toBe("4");
    expect(sent?.nx as number).toBeCloseTo(0.5, 6);
  });

  it("is recorded against the attempt when the pane is torn down mid-session", async () => {
    // The recorder batches, so a pane that closes without flushing loses exactly
    // the interactions somebody was mid-way through making.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.clickAt(225, 338);

    await act(async () => cleanup());

    const posted = h.calls.find((c) => c.url === "/api/sessions/A-1/events");
    const events = (posted?.body ?? []) as { kind: string; target: Record<string, string>; url?: string }[];
    // Down and up are recorded SEPARATELY; the backend folds them into a click
    // only when they share a target and land close together. Synthesising the
    // release (what this used to do) made every drag look like a click.
    expect(events.map((e) => e.kind)).toEqual(["mouseDown", "mouseUp"]);
    expect(events[0].target.testId, "a pixel is not a locator").toBe("add-to-cart");
    expect(events[1].url, "the URL comes from the ack, not from what we believed").toBe("https://shop.gym.local/");
  });
});

// --------------------------------------------------------------------------- ordering

describe("a click faster than the describe round trip", () => {
  it("reaches the service as press-then-release, not the other way round", async () => {
    // The press is only dispatched once describe() has named the element under
    // it. A release arriving inside that round trip used to go out FIRST: the
    // service saw a release with no press, the backend folded no click out of
    // the pair, and the whole interaction left no step, no dropped count and no
    // alert — the annotator's click simply never happened.
    let answer = () => {};
    const describing = new Promise<void>((r) => { answer = r; });
    const h = await mountPane({}, (url) => (url.endsWith("/describe") ? describing : undefined));
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await act(async () => {
      fireEvent(h.surface, new MouseEvent("pointerdown", { bubbles: true, clientX: 225, clientY: 338 }));
      fireEvent(h.surface, new MouseEvent("pointerup", { bubbles: true, clientX: 225, clientY: 338 }));
    });
    expect(h.sock().messages, "nothing may go out while the press is still being named").toHaveLength(0);

    await act(async () => { answer(); await describing; });

    const mouse = h.sock().messages.filter((m) => m.type === "mouse");
    expect(mouse.map((m) => m.phase)).toEqual(["down", "up"]);
    expect(mouse.map((m) => m.id), "and the ids the service judges staleness by follow the same order")
      .toEqual([1, 2]);
  });

  it("still tells a drag from a click", async () => {
    // The guard on the fix above: the release is paired with the press's OWN
    // point and target, which is the only thing that lets the backend fold a
    // click rather than commit a drag as one. Waiting on the press must not cost
    // that pairing.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });

    await act(async () => {
      fireEvent(h.surface, new MouseEvent("pointerdown", { bubbles: true, clientX: 90, clientY: 56 }));
    });
    await act(async () => {
      fireEvent(h.surface, new MouseEvent("pointerup", { bubbles: true, clientX: 450, clientY: 400 }));
    });
    for (const m of h.sock().messages.filter((m) => typeof m.id === "number")) {
      await h.server({ type: "ack", id: m.id, applied: true, state: { url: "https://shop.gym.local/", tabId: "t1" } });
    }
    await act(async () => cleanup());

    const posted = h.calls.find((c) => c.url === "/api/sessions/A-1/events");
    const events = (posted?.body ?? []) as { kind: string; payload: Record<string, number> }[];
    const up = events.find((e) => e.kind === "mouseUp");
    expect(up?.payload.fromNx, "a release with no origin is a click, whatever the annotator did").toBeCloseTo(0.1, 6);
    expect(up?.payload.nx).toBeCloseTo(0.5, 6);
  });
});

// --------------------------------------------------------------------------- recording loss

describe("interactions that can never be recorded", () => {
  it("are reported to the annotator, not left as an unacked counter", async () => {
    // A dispatched input may well have moved the world; only its ack could have
    // turned it into a step. "unacked 1" is a number in a status bar — it does
    // not say the trajectory now has a hole in it, and the alert that does say
    // so could never fire because nothing passed it a count.
    const dropped: number[] = [];
    const h = await mountPane({ onDropped: (n) => dropped.push(n) });
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(225, 338);

    await h.drop(1006);

    expect(screen.getByText("unacked 1")).toBeDefined();
    expect(dropped[dropped.length - 1], "the world moved and the trajectory cannot say why").toBe(1);
  });

  it("are flushed with a beacon when the annotator closes the tab", async () => {
    // A fetch started during unload dies with the document, so a fill still
    // inside the 1.2s batch window — plus the click that terminated it — used to
    // go with it. sendBeacon is the only transport that survives.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await act(async () => { fireEvent.keyDown(h.surface, { key: "m" }); });
    for (const m of h.sock().messages.filter((m) => typeof m.id === "number")) {
      await h.server({ type: "ack", id: m.id, applied: true, state: { url: "https://shop.gym.local/", focus: { value: "m" } } });
    }
    // Keystrokes deliberately do not flush on a boundary, so the fill is still
    // held in memory at this point — which is the whole exposure.
    expect(h.calls.some((c) => c.url === "/api/sessions/A-1/events")).toBe(false);

    const beacons: string[] = [];
    vi.stubGlobal("navigator", { sendBeacon: (url: string) => { beacons.push(String(url)); return true; } });
    await act(async () => { window.dispatchEvent(new Event("pagehide")); });

    expect(beacons).toEqual(["/api/sessions/A-1/events"]);
  });
});
