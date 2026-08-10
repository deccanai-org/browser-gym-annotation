/**
 * Live browser client — the wire the annotator watches and drives.
 *
 * This is the same browser the agent uses, so every rule below exists because
 * breaking it produces a silent lie rather than an error:
 *
 * * Points on the wire are FRACTIONS of the rendered surface (0..1), never
 *   pixels. The surface is almost never the 1280x800 viewport, so a client that
 *   ships pixels mis-places every click by the scale factor and nothing anywhere
 *   reports a failure.
 * * Frames are latest-wins. A queued frame is a picture of a page the annotator
 *   has already left, and clicking on it targets the wrong element.
 * * Input ids are monotonic PER SESSION, not per connection: the service keeps
 *   `last_input_id` on the session and drops anything <= it as stale, so a client
 *   that restarts its counter after a reconnect has every input silently ignored.
 * * A dead socket must be loud. An annotator clicking into a socket that is gone
 *   and seeing nothing happen is the worst failure this component has.
 *
 * The live service is a SEPARATE origin (default :8877). It now ships CORS (see
 * live_browser/service.py), so the REST half works cross-origin when the service
 * allow-lists this app's origin; the websocket Origin-checks against
 * LIVE_ALLOWED_ORIGINS. In a hosted deploy, point this at the live-browser
 * service's public URL by BUILDING the frontend with VITE_LIVE_BASE set; local
 * dev keeps the :8877 default.
 */

export const DEFAULT_LIVE_BASE =
  (import.meta.env.VITE_LIVE_BASE as string | undefined)?.trim() || "http://localhost:8877";

export interface Viewport {
  width: number;
  height: number;
}

/** The service's own default (LIVE_VIEWPORT_W/H); `hello` carries the real one. */
export const DEFAULT_VIEWPORT: Viewport = { width: 1280, height: 800 };

// --------------------------------------------------------------------------- geometry

export interface NormPoint {
  nx: number;
  ny: number;
}

/** The part of a DOMRect this module needs — spelled out so the maths is testable
 *  without a DOM. */
export interface SurfaceRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/**
 * A pointer position → the fraction of the rendered surface it landed on.
 *
 * The whole wire format is fractional for this one reason: the surface is scaled
 * to whatever space the pane has, the remote viewport is not, and a fraction is
 * the only representation that survives the difference. A zero-sized surface
 * (unmounted, or a frame that hasn't arrived) would divide by zero and send NaN,
 * which JSON-encodes to null and lands the click at the page origin — so it
 * clamps to 0 instead.
 */
export function normalizePoint(clientX: number, clientY: number, rect: SurfaceRect): NormPoint {
  return {
    nx: clamp01(rect.width > 0 ? (clientX - rect.left) / rect.width : 0),
    ny: clamp01(rect.height > 0 ? (clientY - rect.top) / rect.height : 0),
  };
}

/**
 * Wheel/scroll deltas travel as PAGE pixels (the service hands them straight to
 * CDP), while points travel as fractions. A wheel tick measured on a 900px-wide
 * surface therefore has to be re-expressed in the remote viewport's pixels, or
 * the page scrolls by a different amount than the annotator's gesture implied.
 */
export function scaleDelta(delta: number, renderedPx: number, pagePx: number): number {
  if (!(renderedPx > 0) || !Number.isFinite(delta)) return 0;
  return (delta * pagePx) / renderedPx;
}

// --------------------------------------------------------------------------- urls

function absolute(base: string, origin?: string): string {
  const b = base.replace(/\/+$/, "");
  if (!b.startsWith("/")) return b;
  return `${(origin ?? "").replace(/\/+$/, "")}${b}`;
}

/** The stream URL for a session. `http`→`ws` and `https`→`wss` so a proxied,
 *  TLS-terminated deploy doesn't try to open an insecure socket from a secure
 *  page (which browsers block outright). */
export function streamUrl(
  base: string,
  sessionId: string,
  opts: { ticket: string; control?: boolean; origin?: string },
): string {
  const wsBase = absolute(base, opts.origin).replace(/^http/, "ws");
  const control = opts.control === false ? "false" : "true";
  return `${wsBase}/live/stream/${encodeURIComponent(sessionId)}?ticket=${encodeURIComponent(opts.ticket)}&control=${control}`;
}

export const RECONNECT_BASE_MS = 300;
export const RECONNECT_CAP_MS = 10_000;

/** Exponential, capped, deterministic. Deterministic because a reconnect cadence
 *  the annotator can predict is the difference between "it's coming back" and
 *  "it's dead"; there is only ever one client per session, so jitter buys
 *  nothing. */
export function backoffMs(attempt: number): number {
  const n = Math.max(1, Math.floor(attempt));
  return Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** (n - 1));
}

// --------------------------------------------------------------------------- rest

export interface RestOptions {
  base?: string;
  fetchImpl?: typeof fetch;
}

export interface OpenedSession {
  sessionId: string;
  ticket: string;
  viewport: Viewport;
}

async function json<T>(
  url: string,
  body: unknown,
  opts: RestOptions | undefined,
  method = "POST",
): Promise<T | null> {
  const f = opts?.fetchImpl ?? fetch;
  try {
    const res = await f(url, {
      method,
      headers: { "content-type": "application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

function liveBase(opts?: RestOptions): string {
  return (opts?.base ?? DEFAULT_LIVE_BASE).replace(/\/+$/, "");
}

/** Open a browser session. Returns null when the live service is unreachable —
 *  the caller renders a disabled pane rather than a spinner that never ends. */
export async function openLiveSession(
  url: string,
  owner: string,
  opts?: RestOptions,
): Promise<OpenedSession | null> {
  const out = await json<{ session_id: string; ticket: string; viewport?: Viewport }>(
    `${liveBase(opts)}/live/sessions`,
    { url, owner },
    opts,
  );
  if (!out?.session_id || !out.ticket) return null;
  return { sessionId: out.session_id, ticket: out.ticket, viewport: out.viewport ?? DEFAULT_VIEWPORT };
}

export async function closeLiveSession(sessionId: string, opts?: RestOptions): Promise<boolean> {
  const out = await json<{ ok: boolean }>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/close`,
    {},
    opts,
  );
  return !!out?.ok;
}

export interface LiveInfo {
  url: string;
  tabs: string[];
  activeTab: number;
  viewport: Viewport;
  frameSeq: number;
}

export async function liveSessionInfo(sessionId: string, opts?: RestOptions): Promise<LiveInfo | null> {
  return json<LiveInfo>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}`,
    undefined,
    opts,
    "GET",
  );
}

/** Locator candidates at a normalized point. Every field the recorder's
 *  redaction and `semantic_locator` read (`type`, `name`, `autocomplete`,
 *  `testId`, `role`, `label`) comes from here, which is why a recorded click
 *  carries a semantic target instead of a pixel. Read BEFORE dispatching: once
 *  the action lands the element may not exist. */
export async function describeAt(
  sessionId: string,
  ticket: string,
  p: NormPoint,
  opts?: RestOptions,
): Promise<Record<string, string>> {
  const out = await json<Record<string, string>>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/describe`,
    { x: p.nx, y: p.ny, ticket },
    opts,
  );
  return out ?? {};
}

/** The text currently selected in the remote page.
 *
 *  Read at pointer-UP, not tracked continuously: a selection only means anything
 *  once it is finished, and polling it would cost a round trip per pointer move.
 *  It is what distinguishes a READING from a drag — at the wire level both are a
 *  press, a move and a release, and a selection recorded as a drag makes the
 *  whole trajectory unshippable because the executor has no drag action.
 */
export async function readSelection(
  sessionId: string,
  ticket: string,
  opts?: RestOptions,
): Promise<string> {
  const out = await json<{ text: string }>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/selection`,
    { ticket },
    opts,
  );
  return out?.text ?? "";
}

/** Size the viewport so the WHOLE page fits, with no scrolling at all.
 *
 *  Iterative server-side, because narrowing the viewport reflows the page taller
 *  — measured on the ShopGym cart, 1280 to 1128 wide took the content from
 *  1378px to 1956px — so one measure-then-resize lands on a height that is
 *  already wrong. `whole` says whether it actually succeeded: a page that keeps
 *  growing (lazy content that renders as the viewport grows) or one taller than
 *  the service will allow cannot be shown whole, and the caller should say so
 *  rather than quietly still scrolling.
 */
export async function fitWholePage(
  sessionId: string,
  ticket: string,
  width: number,
  opts?: RestOptions,
): Promise<{ viewport: Viewport; whole: boolean } | null> {
  const out = await json<{ width: number; height: number; whole: boolean }>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/fit-page`,
    { width: Math.round(width), ticket },
    opts,
  );
  return out && out.width
    ? { viewport: { width: out.width, height: out.height }, whole: !!out.whole }
    : null;
}

/** Reshape the remote viewport to the pane's stage.
 *
 *  The viewport was fixed at 1280x800 while the stage it renders into is a
 *  different shape, so fitting letterboxed it — about 790px of blank margin on a
 *  wide pane, with the page drawn at 63%. Matching the shape makes the scale 1.0
 *  and the bars disappear. Returns the size the service actually adopted, which
 *  is clamped, so the caller must use the ANSWER rather than what it asked for.
 */
export async function setViewport(
  sessionId: string,
  ticket: string,
  width: number,
  height: number,
  opts?: RestOptions,
): Promise<Viewport | null> {
  const out = await json<{ width: number; height: number }>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/viewport`,
    { width: Math.round(width), height: Math.round(height), ticket },
    opts,
  );
  return out && out.width ? { width: out.width, height: out.height } : null;
}

/** The <select> under a point, with its options — or null when there is none.
 *
 *  A native dropdown is painted by the BROWSER, not the page, so the headless
 *  Chromium behind the screencast never renders one and the annotator sees
 *  nothing happen when they click a quantity box. The pane draws the list itself;
 *  this is what it draws. Every task whose answer runs through a <select> — a
 *  quantity, a ship-to address — was impossible to annotate without it.
 */
export interface RemoteSelect {
  value: string;
  multiple: boolean;
  options: { value: string; label: string; selected: boolean; disabled: boolean }[];
}

export async function selectAt(
  sessionId: string,
  ticket: string,
  p: NormPoint,
  opts?: RestOptions,
): Promise<RemoteSelect | null> {
  const out = await json<RemoteSelect>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/select-at`,
    { x: p.nx, y: p.ny, ticket },
    opts,
  );
  return out && Array.isArray(out.options) && out.options.length ? out : null;
}

/** Locator candidates for whatever has KEYBOARD focus in the remote browser.
 *
 *  A client cannot infer this. It knows where the human last clicked, but focus
 *  also moves by Tab, by Enter submitting and advancing, and by a page's own
 *  autofocus — all inside the remote browser. Attributing keystrokes to the last
 *  CLICKED element is how a password typed into a Tab-reached field ends up
 *  recorded against the email field, where the backend's redaction cannot see
 *  it. Returns `{}` when nothing is focused, which the backend treats as
 *  sensitive rather than safe. */
export async function describeFocused(
  sessionId: string,
  ticket: string,
  opts?: RestOptions,
): Promise<Record<string, string>> {
  const out = await json<Record<string, string>>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/focused`,
    { ticket },
    opts,
  );
  return out ?? {};
}

/** What the page looked like: url, title, viewport, scroll, visible text and the
 *  inventory of interactive elements.
 *
 *  Every element is described by the same function that describes the target of
 *  an action, so an element here and the action's own target share a `targetKey`
 *  — which is what lets a consumer find the thing that was clicked inside the
 *  observation, rather than inferring it from coordinates. `{}` when the page
 *  could not be read; the caller records nothing rather than losing the step. */
export async function observePage(
  sessionId: string,
  ticket: string,
  opts?: RestOptions,
): Promise<Record<string, unknown>> {
  const out = await json<Record<string, unknown>>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/observe`,
    { ticket },
    opts,
  );
  return out ?? {};
}

export interface ActResult {
  ok: boolean;
  kind?: string;
  error?: string;
  resolved?: { selector?: string; url?: string };
}

/** Execute one STRUCTURED action — the replay path, used to validate a locator
 *  the human captured rather than to drive the browser (that goes over the
 *  socket). */
export async function act(
  sessionId: string,
  ticket: string,
  body: { kind: string; locator?: Record<string, unknown>; args?: Record<string, unknown> },
  opts?: RestOptions,
): Promise<ActResult> {
  const out = await json<ActResult>(
    `${liveBase(opts)}/live/sessions/${encodeURIComponent(sessionId)}/act`,
    { ...body, ticket },
    opts,
  );
  return out ?? { ok: false, error: "live browser unreachable" };
}

// --------------------------------------------------------------------------- socket

export type LiveStatus = "idle" | "connecting" | "live" | "reconnecting" | "closed";

export interface LiveState {
  status: LiveStatus;
  /** True only for the ONE socket the service granted control to. */
  controller: boolean;
  viewport: Viewport;
  frameSeq: number;
  /** Reconnect attempt number; 0 while connected. */
  attempt: number;
  /** Sent, not yet acked. */
  pendingInputs: number;
  /** Dispatched but never acked because the socket died under them — they may or
   *  may not have applied, which is exactly why they are counted separately. */
  unackedInputs: number;
  /** Refused outright: no socket, read-only, or the service said applied:false. */
  droppedInputs: number;
  lastInputId: number;
  /** The last honest reason for a refusal, denial, close or server error. */
  detail: string | null;
}

export function idleLiveState(viewport: Viewport = DEFAULT_VIEWPORT): LiveState {
  return {
    status: "idle",
    controller: false,
    viewport,
    frameSeq: 0,
    attempt: 0,
    pendingInputs: 0,
    unackedInputs: 0,
    droppedInputs: 0,
    lastInputId: 0,
    detail: null,
  };
}

/**
 * Close codes the service uses BEFORE accepting. Every one of them is terminal:
 * reconnecting against an expired ticket or a session that no longer exists is
 * an infinite spinner, so these stop the loop and say why.
 */
export const FATAL_CLOSE: Record<number, string> = {
  4401: "ticket rejected — expired or minted for another owner; reopen the session",
  4403: "origin refused by the live service (LIVE_ALLOWED_ORIGINS)",
  4404: "the live session is gone",
};

export type SocketFactory = (url: string) => WebSocket;

export interface Timers {
  set: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clear: (handle: ReturnType<typeof setTimeout>) => void;
}

const REAL_TIMERS: Timers = {
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (h) => clearTimeout(h),
};

export interface LiveSocketConfig {
  sessionId: string;
  ticket: string;
  base?: string;
  origin?: string;
  /** Ask for control. The first such socket wins it; the rest are viewers. */
  control?: boolean;
  socketFactory?: SocketFactory;
  timers?: Timers;
  /** Idle sockets die quietly behind proxies; the pong is how we find out. 0 disables. */
  pingMs?: number;
  onState?: (s: LiveState) => void;
  /** Frames bypass onState so a 60fps stream doesn't re-render the pane. */
  onFrame?: (f: { seq: number; data: string }) => void;
  /** An input that was APPLIED, with the state it produced. The only place
   *  interactions should be recorded from. */
  onRecord?: (ev: RecordedEvent) => void;
  /** Inputs that were dispatched and can now never be recorded, because the
   *  socket died before their ack came back. Distinct from `unackedInputs`,
   *  which is a number in the counter bar: these inputs may well have moved the
   *  world, so the trajectory has a hole in it and only the annotator can decide
   *  whether the task has to be redone. Called with the count, not a total. */
  onRecordLoss?: (n: number) => void;
  /** Something the page did on its own (a popup, a redirect) — no ack to ride on. */
  onNotice?: (n: Record<string, unknown>) => void;
}

/** `WebSocket.readyState` values, spelled out rather than read off the global:
 *  the constructor is stubbed wholesale in tests, and a fake without the
 *  constants would make every comparison undefined. */
const WS_CLOSING = 2;
const WS_CLOSED = 3;

export class LiveSocket {
  private sock: WebSocket | null = null;
  private state: LiveState;
  private pending = new Set<number>();
  // input id -> how to record it, resolved when (and only when) its ack says applied
  private pendingRecords = new Map<number, RecordFactory>();
  private reconnectHandle: ReturnType<typeof setTimeout> | null = null;
  private pingHandle: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private readonly timers: Timers;

  constructor(private readonly cfg: LiveSocketConfig) {
    this.timers = cfg.timers ?? REAL_TIMERS;
    this.state = idleLiveState();
  }

  get snapshot(): LiveState {
    return this.state;
  }

  connect(): void {
    if (this.sock) return;
    this.stopped = false;
    this.patch({ status: this.state.attempt > 0 ? "reconnecting" : "connecting" });
    const url = streamUrl(this.cfg.base ?? DEFAULT_LIVE_BASE, this.cfg.sessionId, {
      ticket: this.cfg.ticket,
      control: this.cfg.control,
      origin: this.cfg.origin,
    });
    const factory = this.cfg.socketFactory ?? ((u: string) => new WebSocket(u));
    let sock: WebSocket;
    try {
      sock = factory(url);
    } catch (err) {
      this.scheduleReconnect(`could not open the stream: ${String(err)}`);
      return;
    }
    this.sock = sock;
    sock.onmessage = (ev) => this.onMessage(String(ev.data));
    sock.onerror = () => this.patch({ detail: "stream error" });
    sock.onclose = (ev) => this.onClose(ev.code);
  }

  /** Intentional teardown: never reconnects, and says so. */
  disconnect(): void {
    this.stopped = true;
    this.clearTimers();
    const sock = this.sock;
    this.sock = null;
    if (sock) {
      sock.onmessage = null;
      sock.onclose = null;
      sock.onerror = null;
      try {
        sock.close();
      } catch {
        /* already gone */
      }
    }
    this.loseRecords();
    this.patch({ status: "closed", controller: false, attempt: 0, pendingInputs: 0 });
  }

  /**
   * Give up on every ack still outstanding, and say how many steps that costs.
   *
   * An input whose ack never arrives may still have applied — a recorded world
   * that moved with no step explaining why is exactly the corruption this whole
   * component exists to avoid — so the count goes to the annotator rather than
   * being left as an "unacked" number they cannot act on. Emptying the map is
   * also what stops a long session accumulating a factory per dead input.
   */
  private loseRecords(): void {
    const lost = this.pendingRecords.size;
    if (!lost) return;
    this.pendingRecords.clear();
    this.cfg.onRecordLoss?.(lost);
  }

  /** Reconnect now, without waiting out the backoff (the "Reconnect" button). */
  retry(): void {
    if (this.sock) return;
    this.clearTimers();
    this.patch({ attempt: 0, detail: null });
    this.connect();
  }

  click(p: NormPoint, opts?: { button?: string; clicks?: number }): boolean {
    return this.send({ type: "click", nx: p.nx, ny: p.ny, button: opts?.button ?? "left", clicks: opts?.clicks ?? 1 });
  }

  move(p: NormPoint): boolean {
    return this.send({ type: "move", nx: p.nx, ny: p.ny });
  }

  scroll(p: NormPoint, dy: number, dx = 0): boolean {
    return this.send({ type: "scroll", nx: p.nx, ny: p.ny, dy, dx });
  }

  typeText(text: string): boolean {
    return this.send({ type: "type", text });
  }

  /** Browser history. The service has always understood these; the pane simply
   *  had no buttons, so an annotator who followed a link had no way back and had
   *  to retype a URL — in a task where the URL is a per-session sid, that is not
   *  a realistic thing to ask of them. */
  history(which: "back" | "forward" | "reload", record?: RecordFactory): boolean {
    return this.send({ type: which }, record);
  }

  /** Choose `value` in the <select> at `p`. See selectAt for why this exists. */
  select(p: NormPoint, value: string, record?: RecordFactory): boolean {
    return this.send({ type: "select", nx: p.nx, ny: p.ny, value }, record);
  }

  key(key: string): boolean {
    return this.send({ type: "key", key });
  }

  navigate(url: string): boolean {
    return this.send({ type: "navigate", url });
  }

  /**
   * Ids come from ONE counter for the life of the session and never reset on
   * reconnect: the service compares against `last_input_id`, which lives on the
   * session, so a counter that restarts at 1 has every input answered
   * `applied:false, reason:"stale"` — input that looks delivered and isn't.
   */
  send(msg: Record<string, unknown>, record?: RecordFactory): boolean {
    if (!this.sock || this.state.status !== "live") {
      return this.refuse("not connected — that input was NOT delivered");
    }
    if (!this.state.controller) {
      return this.refuse("read-only viewer — another connection holds control");
    }
    // A socket in CLOSING/CLOSED does NOT throw from send(); per spec it discards
    // the payload and bumps bufferedAmount. `status` is still "live" until the
    // close event lands, so those inputs used to look delivered, sit in `pending`
    // until onClose swept them into "unacked", and never record anything.
    const readyState = (this.sock as { readyState?: number }).readyState;
    if (readyState === WS_CLOSING || readyState === WS_CLOSED) {
      return this.refuse("the stream is closing — that input was NOT delivered");
    }
    const id = this.state.lastInputId + 1;
    try {
      this.sock.send(JSON.stringify({ ...msg, id }));
    } catch (err) {
      return this.refuse(`could not send: ${String(err)}`);
    }
    if (record) this.pendingRecords.set(id, record);
    this.pending.add(id);
    this.patch({ lastInputId: id, pendingInputs: this.pending.size });
    return true;
  }

  private refuse(detail: string): boolean {
    this.patch({ droppedInputs: this.state.droppedInputs + 1, detail });
    return false;
  }

  private onMessage(raw: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      this.patch({ detail: "unparseable frame from the live service" });
      return;
    }
    switch (msg.type) {
      case "hello": {
        const vp = (msg.viewport as Viewport | undefined) ?? DEFAULT_VIEWPORT;
        this.patch({
          status: "live",
          controller: !!msg.controller,
          viewport: vp,
          attempt: 0,
          detail: msg.controller ? null : "read-only viewer — another connection holds control",
        });
        this.schedulePing();
        break;
      }
      case "frame": {
        const seq = Number(msg.seq ?? 0);
        // Latest-wins, never queued: an older frame is a picture of a page the
        // annotator already left, and clicking it targets the wrong element.
        if (seq <= this.state.frameSeq) return;
        this.state = { ...this.state, frameSeq: seq };
        this.cfg.onFrame?.({ seq, data: String(msg.data ?? "") });
        break;
      }
      case "ack": {
        const id = Number(msg.id ?? 0);
        this.pending.delete(id);
        const applied = msg.applied === true;
        const rec = this.pendingRecords.get(id);
        this.pendingRecords.delete(id);
        // Record ONLY what actually applied, and stamp it with the state the
        // service reports — not with what we believed we were sending.
        if (applied && rec) {
          const ev = rec((msg.state as AckState | undefined) ?? null);
          if (ev) this.cfg.onRecord?.(ev);
        }
        this.patch({
          pendingInputs: this.pending.size,
          ...(applied
            ? {}
            : {
                droppedInputs: this.state.droppedInputs + 1,
                detail: `input ${id} was not applied (${String(msg.reason ?? "unknown")})`,
              }),
        });
        break;
      }
      case "notice":
        // The page acted on its own (popup, redirect). No ack carries it, so the
        // pane would otherwise have no idea it happened.
        this.cfg.onNotice?.(msg);
        break;
      case "denied":
        this.patch({ controller: false, detail: `input refused: ${String(msg.reason ?? "read-only viewer")}` });
        break;
      case "error":
        this.patch({ detail: String(msg.detail ?? "live service error") });
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  private onClose(code: number): void {
    this.sock = null;
    this.clearTimers();
    // These were dispatched and never answered. They may have applied — that
    // ambiguity is the point, so they are counted apart from refusals.
    const unacked = this.state.unackedInputs + this.pending.size;
    this.pending.clear();
    this.loseRecords();
    this.patch({ controller: false, pendingInputs: 0, unackedInputs: unacked });
    if (this.stopped) {
      this.patch({ status: "closed" });
      return;
    }
    const fatal = FATAL_CLOSE[code];
    if (fatal) {
      this.stopped = true;
      this.patch({ status: "closed", detail: fatal });
      return;
    }
    this.scheduleReconnect(`stream closed (${code}) — reconnecting`);
  }

  private scheduleReconnect(detail: string): void {
    this.sock = null;
    const attempt = this.state.attempt + 1;
    this.patch({ status: "reconnecting", attempt, detail });
    this.reconnectHandle = this.timers.set(() => {
      this.reconnectHandle = null;
      if (!this.stopped) this.connect();
    }, backoffMs(attempt));
  }

  private schedulePing(): void {
    const every = this.cfg.pingMs ?? 20_000;
    if (!every) return;
    this.pingHandle = this.timers.set(() => {
      this.pingHandle = null;
      if (this.sock && this.state.status === "live") {
        try {
          // `ping` carries no id and is answered for viewers too, so it never
          // burns an input id or trips the stale check.
          this.sock.send(JSON.stringify({ type: "ping" }));
        } catch {
          /* the close handler will pick this up */
        }
        this.schedulePing();
      }
    }, every);
  }

  private clearTimers(): void {
    if (this.reconnectHandle !== null) {
      this.timers.clear(this.reconnectHandle);
      this.reconnectHandle = null;
    }
    if (this.pingHandle !== null) {
      this.timers.clear(this.pingHandle);
      this.pingHandle = null;
    }
  }

  private patch(next: Partial<LiveState>): void {
    this.state = { ...this.state, ...next };
    this.cfg.onState?.(this.state);
  }
}

// --------------------------------------------------------------------------- recorder

/**
 * What the human did, on its way to `POST /api/sessions/{id}/events`.
 *
 * The kinds are chosen to fold correctly in `recorder.coalesce` on the backend:
 * `mousePressed` + `mouseReleased` on one target become a single `click`;
 * consecutive `key` events on one target become one `fill` carrying the typed
 * text; `press`, `scroll` and `navigate` pass through untouched. Naming a
 * keystroke `press` (or Enter `key`) would fold the two into each other and
 * produce a step that types the Enter key into the field.
 */
export interface RecordedEvent {
  kind: string;
  payload?: Record<string, unknown>;
  target?: Record<string, unknown>;
  url?: string;
  tab?: string;
}

/**
 * What the service reports was true AFTER an input was applied.
 *
 * This is the whole point of recording on the ack. The client only knows what it
 * SENT; it cannot know the URL a click navigated to, which tab the input landed
 * in, or what a field's value became once Backspace and autocomplete had their
 * say. Recording from here instead of from intent is what makes the trajectory
 * describe what happened rather than what we hoped would happen.
 */
export interface AckState {
  url?: string;
  tabId?: string;
  tabIndex?: number;
  frameSeq?: number;
  focus?: Record<string, unknown>;
}

/** Builds the event to record once the ack says the input actually applied.
 *  Return null to record nothing (e.g. a no-op move). */
export type RecordFactory = (state: AckState | null) => RecordedEvent | null;

interface EventBody {
  kind: string;
  payload: Record<string, unknown>;
  target: Record<string, unknown>;
  url: string;
  tab: string;
  /** Minted here, so a retry is recognised as the SAME event.
   *
   *  `flush` re-queues a whole batch on any error — including a network drop
   *  AFTER the server committed it. Without an id the retry appended a second
   *  copy of every event in that batch, and the duplicates then folded into
   *  doubled clicks and doubled fills. */
  clientEventId: string;
}

/** Unique per recorded event. crypto.randomUUID where available (every browser
 *  we target), with a counter fallback so tests and older runtimes still get a
 *  distinct id rather than silently colliding. */
let _evtCounter = 0;
function newEventId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  _evtCounter += 1;
  return `e-${Date.now().toString(36)}-${_evtCounter}`;
}

export const EVENT_BATCH_AT = 40;
export const EVENT_FLUSH_MS = 1200;

/** Kinds that CLOSE an action, so the batch holding them should go now — see
 *  `EventRecorder.push`. A click, a navigation and a tab switch are each
 *  complete the moment they ack; a keystroke is not. */
export const BOUNDARY_KINDS = new Set(["mouseUp", "mouseReleased", "navigate", "switch_tab"]);
/** Above this the backend is clearly gone; keep a contiguous PREFIX and count
 *  the rest, because a hole in the middle of the stream mis-folds (a
 *  `mousePressed` whose `mouseReleased` was dropped becomes a bogus `press`). */
export const EVENT_QUEUE_CAP = 600;

export interface EventRecorderConfig {
  attemptId: string;
  fetchImpl?: typeof fetch;
  timers?: Timers;
  batchAt?: number;
  flushMs?: number;
  now?: () => number;
  onDrop?: (dropped: number) => void;
}

/** One frame the annotator saw, keyed to the interaction it belongs to. */
export interface RecordedFrame {
  clientEventId: string;
  jpegBase64: string;
  width: number;
  height: number;
}

/** What the page looked like, keyed to the interaction it belongs to. */
export interface RecordedObservation {
  clientEventId: string;
  observation: Record<string, unknown>;
}

/**
 * Uploads bulky per-step evidence on its OWN channel.
 *
 * Deliberately not part of the event batch: a frame is ~60KB, and because the
 * event recorder re-queues a failed batch whole, one failed upload would replay
 * the entire interaction stream. These are best-effort — losing a picture or an
 * observation is survivable, losing the interactions is not — so this drops
 * rather than retrying forever.
 */
class SideChannel<T> {
  private queue: T[] = [];
  private handle: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly cfg: { attemptId: string; base?: string; timers?: Timers },
    private readonly path: string,
    private readonly cap: number,
  ) {}

  push(item: T): void {
    if (this.queue.length >= this.cap) this.queue.shift();   // keep the newest
    this.queue.push(item);
    const timers = this.cfg.timers ?? REAL_TIMERS;
    if (this.handle) return;
    this.handle = timers.set(() => {
      this.handle = null;
      void this.flush();
    }, 900);
  }

  async flush(): Promise<void> {
    const batch = this.queue.splice(0, this.queue.length);
    if (!batch.length) return;
    try {
      await fetch(`${this.cfg.base ?? ""}/api/sessions/${this.cfg.attemptId}/${this.path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify(batch),
      });
    } catch {
      /* a lost side-channel payload must never disturb the interaction stream */
    }
  }
}

export class FrameRecorder extends SideChannel<RecordedFrame> {
  constructor(cfg: { attemptId: string; base?: string; timers?: Timers }) {
    super(cfg, "frames", 24);
  }
}

/**
 * The OBSERVATION half of a trajectory — url, title, viewport, scroll, visible
 * text and the inventory of interactive elements.
 *
 * Without it a sample says what the annotator did and nothing about what they
 * could see, and a policy cannot be trained on the action alone. Captured on the
 * ack like a frame, so it is the page AFTER the action: step N's observation is
 * step N+1's input, and the session's opening observation covers step 0.
 *
 * A smaller queue than frames because an observation is the bigger payload.
 */
export class ObservationRecorder extends SideChannel<RecordedObservation> {
  constructor(cfg: { attemptId: string; base?: string; timers?: Timers }) {
    super(cfg, "observations", 12);
  }
}

export class EventRecorder {
  private queue: EventBody[] = [];
  private handle: ReturnType<typeof setTimeout> | null = null;
  private inFlight: Promise<number> | null = null;
  private droppedCount = 0;
  private readonly timers: Timers;
  private readonly now: () => number;

  constructor(private readonly cfg: EventRecorderConfig) {
    this.timers = cfg.timers ?? REAL_TIMERS;
    this.now = cfg.now ?? (() => Date.now());
  }

  get queued(): number {
    return this.queue.length;
  }

  get dropped(): number {
    return this.droppedCount;
  }

  /** Every way an interaction stops existing goes through here.
   *
   *  The queue cap used to be the only path that reported, so a backend that
   *  accepted half a batch, or a tab closed on a queue that could not be handed
   *  over, lost interactions with the alert still reading zero — which is worse
   *  than no alert, because the annotator ships the sample believing it whole. */
  private drop(n: number): void {
    if (n <= 0) return;
    this.droppedCount += n;
    this.cfg.onDrop?.(this.droppedCount);
  }

  /**
   * Queue one raw event. Never sent alone: a keystroke per request would put a
   * round trip between the annotator and every character they type.
   */
  push(ev: RecordedEvent): string | null {
    if (this.queue.length >= EVENT_QUEUE_CAP) {
      this.drop(1);
      return null;
    }
    const payload: Record<string, unknown> = { ...ev.payload };
    // `coalesce` pairs a press/release within 700ms and folds keystrokes within
    // 1500ms, reading `payload.t`. Without it every event lands at t=0 and a
    // whole session of typing folds into one fill.
    if (payload.t === undefined) payload.t = this.now();
    const clientEventId = newEventId();
    this.queue.push({
      clientEventId,
      kind: ev.kind,
      payload,
      target: ev.target ?? {},
      url: ev.url ?? "",
      tab: ev.tab ?? "",
    });
    // Send immediately on an action BOUNDARY. The server reads the gym world
    // once per folded batch, so an action that waits out the 1.2s timer shares
    // its observation with whatever the annotator did next, and the state change
    // can only be attributed to the window rather than to the step. Flushing
    // here is what makes a per-step delta observable in the normal case.
    // Deliberately not keyChar: a request per keystroke is what the batch exists
    // to avoid, and an edit is not finished until the field settles anyway.
    if (BOUNDARY_KINDS.has(ev.kind) || this.queue.length >= (this.cfg.batchAt ?? EVENT_BATCH_AT)) {
      void this.flush();
      return clientEventId;
    }
    this.rearm();
    return clientEventId;
  }

  /** Schedule the next flush, unless one is already scheduled. */
  private rearm(): void {
    if (this.handle !== null) return;
    this.handle = this.timers.set(() => {
      this.handle = null;
      void this.flush();
    }, this.cfg.flushMs ?? EVENT_FLUSH_MS);
  }

  /** Returns how many events the server accepted. A failed POST puts the batch
   *  back at the FRONT: the interaction log is append-only and ordered, so
   *  losing a blip's worth of events silently corrupts every action folded
   *  after it. */
  async flush(): Promise<number> {
    if (this.inFlight) return this.inFlight;
    if (this.handle !== null) {
      this.timers.clear(this.handle);
      this.handle = null;
    }
    if (!this.queue.length) return 0;
    const batch = this.queue;
    this.queue = [];
    const f = this.cfg.fetchImpl ?? fetch;
    this.inFlight = (async () => {
      try {
        const res = await f(`/api/sessions/${encodeURIComponent(this.cfg.attemptId)}/events`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          credentials: "include",
          body: JSON.stringify(batch),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const out = (await res.json()) as { recorded?: number; duplicates?: number };
        const recorded = out?.recorded ?? batch.length;
        // A 200 that ACCOUNTED FOR fewer events than it was sent is a partial
        // write: the rest are gone, the batch cannot be retried without
        // duplicating what did land, and nothing else in the pipeline notices.
        //
        // `duplicates` has to count towards that. The server records nothing for
        // an event id it has already seen — deliberately, because this recorder
        // re-queues a whole batch on any failure, including a network drop AFTER
        // the commit, so a retry has to be a no-op. A retry therefore answers
        // {recorded: 0, duplicates: N}, and subtracting only `recorded` reported
        // the whole batch as lost: the annotator is told their trajectory has a
        // hole and redoes a task that was intact. Duplicates are in fact the only
        // way this endpoint can answer short, so ignoring them made this branch
        // fire exclusively on the false positive.
        this.drop(batch.length - recorded - (out?.duplicates ?? 0));
        return recorded;
      } catch {
        this.queue = [...batch, ...this.queue];
        // Re-arm. flush() cleared the pending timer on entry, so without this a
        // single failed POST leaves the batch queued with nothing scheduled to
        // send it — and the recorder goes quiet for the rest of the session
        // unless the annotator happens to generate another BATCH_AT events.
        // Silent, and it loses exactly the interactions somebody is mid-way
        // through recording.
        this.rearm();
        return 0;
      } finally {
        this.inFlight = null;
      }
    })();
    return this.inFlight;
  }

  /**
   * Hand the queue to the browser to finish delivering after the page is gone.
   *
   * A normal fetch started during unload is cancelled along with the document,
   * so closing the tab used to throw away everything still inside the 1.2s flush
   * window — easily a whole fill and the click that terminated it, with nothing
   * anywhere saying the trajectory now ends mid-action. sendBeacon is the only
   * transport the browser promises to complete past unload.
   *
   * Returns false when the queue could not be handed over, in which case the
   * events are counted as lost rather than assumed sent.
   */
  flushBeacon(): boolean {
    if (!this.queue.length) return true;
    const batch = this.queue;
    const nav = (globalThis as { navigator?: { sendBeacon?: (url: string, data?: unknown) => boolean } }).navigator;
    let ok = false;
    try {
      // A Blob, not a string: sendBeacon labels a bare string text/plain and the
      // events endpoint parses by content type, so the batch would 422 on arrival.
      const body = new Blob([JSON.stringify(batch)], { type: "application/json" });
      ok = nav?.sendBeacon?.(`/api/sessions/${encodeURIComponent(this.cfg.attemptId)}/events`, body) === true;
    } catch {
      ok = false;
    }
    if (!ok) {
      this.drop(batch.length);
      return false;
    }
    this.queue = [];
    return true;
  }

  /**
   * Flush on unload. Returns the uninstaller.
   *
   * Both events, because neither alone covers a tab close: `beforeunload` does
   * not fire when a mobile browser freezes the page into the bfcache, and
   * `pagehide` is the one the spec actually guarantees. The beacon is a no-op on
   * an empty queue, so firing twice costs nothing.
   */
  installUnloadFlush(target?: {
    addEventListener: (t: string, fn: () => void) => void;
    removeEventListener: (t: string, fn: () => void) => void;
  }): () => void {
    const w = target ?? (globalThis as unknown as {
      addEventListener?: (t: string, fn: () => void) => void;
      removeEventListener?: (t: string, fn: () => void) => void;
    });
    if (typeof w?.addEventListener !== "function") return () => {};
    const onUnload = () => {
      this.flushBeacon();
    };
    w.addEventListener("pagehide", onUnload);
    w.addEventListener("beforeunload", onUnload);
    return () => {
      w.removeEventListener?.("pagehide", onUnload);
      w.removeEventListener?.("beforeunload", onUnload);
    };
  }

  /** Stop the clock, and account for anything the caller's final flush could not
   *  place. Callers flush first, so a non-empty queue here means the POST came
   *  back and failed onto a recorder nobody holds any more — those events are
   *  never going anywhere, and used to disappear without touching the counter. */
  dispose(): void {
    if (this.handle !== null) {
      this.timers.clear(this.handle);
      this.handle = null;
    }
    const stranded = this.queue.length;
    if (stranded) {
      this.queue = [];
      this.drop(stranded);
    }
  }
}
