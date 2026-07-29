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
 *  pointer click never reaches the socket at all. */
function stubFetch(reply: (url: string) => unknown): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), body: init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown) });
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

async function mountPane(over: Partial<ComponentProps<typeof LiveBrowserPane>> = {}) {
  FakeSocket.opened = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  const calls = stubFetch(REPLY);
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

    await waitFor(() => expect(h.sock().messages.filter((m) => m.type === "click")).toHaveLength(2));
    for (const click of h.sock().messages.filter((m) => m.type === "click")) {
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
    expect(h.sock().messages.map((m) => m.type)).toEqual(["click"]);
  });

  it("is recorded against the attempt when the pane is torn down mid-session", async () => {
    // The recorder batches, so a pane that closes without flushing loses exactly
    // the interactions somebody was mid-way through making.
    const h = await mountPane();
    await h.hello(true);
    sizeSurface(h.surface, { left: 0, top: 0, width: 900, height: 563 });
    await h.pointerAt(225, 338);

    await act(async () => cleanup());

    const posted = h.calls.find((c) => c.url === "/api/sessions/A-1/events");
    const events = (posted?.body ?? []) as { kind: string; target: Record<string, string> }[];
    expect(events.map((e) => e.kind), "a press with no release folds into a bogus step").toEqual(["mousePressed", "mouseReleased"]);
    expect(events[0].target.testId, "a pixel is not a locator").toBe("add-to-cart");
  });
});
