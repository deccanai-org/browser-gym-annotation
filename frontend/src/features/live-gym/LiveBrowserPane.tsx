import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, t, weight } from "../../ds";
import {
  DEFAULT_VIEWPORT,
  EventRecorder,
  LiveSocket,
  describeAt,
  describeFocused,
  idleLiveState,
  liveSessionInfo,
  normalizePoint,
  openLiveSession,
  scaleDelta,
} from "../../lib/liveBrowser";
import type { LiveState, NormPoint, OpenedSession, Viewport } from "../../lib/liveBrowser";

/**
 * The live browser pane — the annotator watches and drives the SAME browser the
 * agent uses, instead of scrubbing screenshots of one that has already finished.
 *
 * Three things here are load-bearing and easy to get subtly wrong:
 *
 * 1. The surface is measured, not CSS-fitted. Every pointer position is a
 *    fraction of THIS element's box, so the box must be exactly the painted
 *    image — any letterboxing inside it silently offsets every click. Fitting in
 *    JS makes that guaranteed rather than dependent on how a browser resolves
 *    `aspect-ratio` against `max-height`.
 * 2. Frames never pass through React state. At 60fps a `setState` per frame
 *    re-renders the whole pane; the socket writes straight to the <img> instead.
 * 3. A socket that is not live is shown as a full-surface blocker, not a subtle
 *    badge. An annotator clicking into a dead stream and seeing nothing happen
 *    is the failure this component exists to prevent.
 */
export function LiveBrowserPane({
  attemptId,
  session: sessionProp,
  startUrl,
  owner,
  base,
  control = true,
  onSession,
}: {
  /** Review-session id — where recorded interactions land. Null disables
   *  recording (offline/fixture mode) but still lets the annotator drive. */
  attemptId: string | null;
  /** An already-minted live session. The ticket can only be minted by whoever
   *  opened the session, so the host passes it down when it opened one. */
  session?: OpenedSession | null;
  /** Fallback: let the pane open its own session against this URL. */
  startUrl?: string;
  owner?: string;
  base?: string;
  /** Ask for control. The first socket that asks gets it; the rest are viewers. */
  control?: boolean;
  onSession?: (s: OpenedSession | null) => void;
}) {
  const [ownSession, setOwnSession] = useState<OpenedSession | null>(null);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState>(idleLiveState(sessionProp?.viewport ?? DEFAULT_VIEWPORT));
  const [pageUrl, setPageUrl] = useState("");
  const [urlDraft, setUrlDraft] = useState("");
  const [focused, setFocused] = useState(false);
  const [fit, setFit] = useState({ w: 0, h: 0 });

  const stageRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const sockRef = useRef<LiveSocket | null>(null);
  const recRef = useRef<EventRecorder | null>(null);
  // The last element the pointer resolved to. Keystrokes have no coordinates, so
  // this is what attributes them to a field — and what lets the backend redact a
  // password before it is ever written to the append-only log.
  const targetRef = useRef<Record<string, string>>({});
  const lastMoveRef = useRef(0);

  // Never mirrored into state, and the socket effect keys off the two PRIMITIVES
  // below rather than this object: a host that inlines `session={{…}}` produces a
  // new identity every render, and an effect that depended on it would tear the
  // stream down and re-race for control on each one.
  const session = sessionProp ?? ownSession;
  const sid = session?.sessionId ?? null;
  const ticket = session?.ticket ?? null;

  const vp: Viewport = live.viewport ?? session?.viewport ?? DEFAULT_VIEWPORT;
  const driving = live.status === "live" && live.controller;

  const refreshInfo = useCallback(
    async (id: string) => {
      const info = await liveSessionInfo(id, { base });
      if (info?.url) {
        setPageUrl(info.url);
        setUrlDraft(info.url);
      }
    },
    [base],
  );

  // --- socket + recorder lifecycle -----------------------------------------
  useEffect(() => {
    if (!sid || !ticket) return;
    const rec = attemptId ? new EventRecorder({ attemptId }) : null;
    recRef.current = rec;
    const sock = new LiveSocket({
      sessionId: sid,
      ticket,
      base,
      control,
      origin: typeof window === "undefined" ? undefined : window.location.origin,
      onState: setLive,
      onFrame: (f) => {
        const img = imgRef.current;
        if (img) img.src = `data:image/jpeg;base64,${f.data}`;
      },
    });
    sockRef.current = sock;
    sock.connect();
    void refreshInfo(sid);
    return () => {
      sock.disconnect();
      // Flush before dropping the queue: a batch that never left the browser is
      // an interaction that never happened as far as the trajectory is concerned.
      void rec?.flush();
      rec?.dispose();
      sockRef.current = null;
      recRef.current = null;
    };
  }, [sid, ticket, attemptId, base, control, refreshInfo]);

  // --- fit the surface to the viewport aspect ------------------------------
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const measure = () => {
      const r = stage.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      const scale = Math.min(r.width / vp.width, r.height / vp.height);
      setFit({ w: Math.floor(vp.width * scale), h: Math.floor(vp.height * scale) });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(stage);
    return () => ro.disconnect();
  }, [vp.width, vp.height]);

  const pointAt = (clientX: number, clientY: number): NormPoint | null => {
    const box = surfaceRef.current;
    if (!box) return null;
    return normalizePoint(clientX, clientY, box.getBoundingClientRect());
  };

  // --- wheel: a native non-passive listener, because React's onWheel cannot
  // preventDefault and the annotator's own page would scroll instead of the
  // remote one.
  useEffect(() => {
    const box = surfaceRef.current;
    if (!box) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const sock = sockRef.current;
      const p = pointAt(e.clientX, e.clientY);
      if (!sock || !p) return;
      const r = box.getBoundingClientRect();
      const dy = scaleDelta(e.deltaY, r.height, vp.height);
      const dx = scaleDelta(e.deltaX, r.width, vp.width);
      if (!sock.scroll(p, dy, dx)) return;
      recRef.current?.push({
        kind: "scroll",
        payload: { dy, dx, nx: p.nx, ny: p.ny, auto: false },
        target: targetRef.current,
        url: pageUrl,
      });
    };
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
    // `sid` is a dependency because the surface only exists once a session does —
    // without it the listener would never attach to a pane that got its session
    // after mount.
  }, [sid, vp.width, vp.height, pageUrl]);

  const onPointerDown = async (e: React.PointerEvent<HTMLDivElement>) => {
    const sock = sockRef.current;
    const p = pointAt(e.clientX, e.clientY);
    if (!sock || !p || !sid || !ticket) return;
    // Describe BEFORE dispatching. Afterwards the element may be gone, and a
    // recorded pixel is not replayable — the committed step needs a locator.
    const target = await describeAt(sid, ticket, p, { base });
    targetRef.current = target;
    focusStaleRef.current = false;  // a click IS a focus change, and we just named it
    const at = Date.now();
    if (!sock.click(p)) return; // refused — never record an action that did not happen
    const ev = { target, url: pageUrl };
    recRef.current?.push({ kind: "mousePressed", payload: { t: at, nx: p.nx, ny: p.ny }, ...ev });
    recRef.current?.push({ kind: "mouseReleased", payload: { t: at + 1, nx: p.nx, ny: p.ny }, ...ev });
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const sock = sockRef.current;
    if (!sock || !driving) return;
    const now = Date.now();
    // Hover matters (menus, tooltips) but every mousemove would burn an input id
    // per pixel and swamp the ack channel.
    if (now - lastMoveRef.current < 60) return;
    lastMoveRef.current = now;
    const p = pointAt(e.clientX, e.clientY);
    if (p) sock.move(p);
  };

  // Whether the remote page's focus may have moved since targetRef was written.
  // Set by anything that can move focus without a click; cleared once the page
  // has told us where focus actually is.
  const focusStaleRef = useRef(true);

  /** Re-read the focused element from the REMOTE page.
   *
   *  The pane cannot infer focus: it knows where the human last clicked, but Tab,
   *  Enter-submits-and-advances, and a page's own autofocus all move focus inside
   *  the remote browser. Attributing keystrokes to the last CLICKED element is
   *  how a password typed into a Tab-reached field gets recorded against the
   *  email field — where the backend's redaction, which keys entirely off the
   *  target, cannot see it. On failure the target is cleared rather than left
   *  stale, because the backend treats an unnamed target as sensitive. */
  const syncFocus = async () => {
    if (!sid || !ticket) return;
    try {
      targetRef.current = await describeFocused(sid, ticket, { base });
    } catch {
      targetRef.current = {};
    }
    focusStaleRef.current = false;
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const sock = sockRef.current;
    if (!sock) return;
    if (e.metaKey || e.ctrlKey) return; // leave the annotator's own shortcuts alone
    if (["Shift", "Alt", "Meta", "Control", "CapsLock"].includes(e.key)) return;
    e.preventDefault();
    const printable = e.key.length === 1;
    // A keystroke we cannot attribute must not be attributed to the WRONG field.
    // Clear first, then re-read asynchronously: this handler cannot await without
    // dropping the keystroke's ordering, and an empty target is redacted by the
    // backend while a stale one is not.
    if (focusStaleRef.current) {
      targetRef.current = {};
      void syncFocus();
    }
    const ev = { target: targetRef.current, url: pageUrl };
    if (printable) {
      if (!sock.typeText(e.key)) return;
      // `key` events fold into ONE fill on the backend. Enter must not be one of
      // them, or the committed step types the Enter key into the field.
      recRef.current?.push({ kind: "key", payload: { text: e.key }, ...ev });
      return;
    }
    if (!sock.key(e.key)) return;
    recRef.current?.push({ kind: "press", payload: { key: e.key }, ...ev });
    // Tab, Enter and the arrows are exactly the keys that move focus.
    focusStaleRef.current = true;
    void syncFocus();
  };

  const go = () => {
    const sock = sockRef.current;
    const url = urlDraft.trim();
    if (!sock || !url) return;
    if (!sock.navigate(url)) return;
    recRef.current?.push({ kind: "navigate", payload: { url }, url: pageUrl });
    setPageUrl(url);
    // The ack is a DISPATCH ack, not a settle ack — the goto has not finished when
    // it arrives, so re-read the URL once the navigation has had time to land.
    if (sid) window.setTimeout(() => void refreshInfo(sid), 1500);
  };

  const open = async () => {
    if (!startUrl) return;
    setOpening(true);
    setOpenError(null);
    const s = await openLiveSession(startUrl, owner ?? "annotator", { base });
    setOpening(false);
    if (!s) {
      // The live service ships no CORS middleware, so this is the expected
      // failure until a same-origin proxy exists. Say that, rather than spinning.
      setOpenError("could not reach the live browser service — check it is running and reachable from this origin");
      return;
    }
    setOwnSession(s);
    onSession?.(s);
  };

  const card: React.CSSProperties = {
    flex: 1,
    minHeight: 0,
    background: t.n9,
    border: `1px solid ${t.n7}`,
    borderRadius: t.radiusXl,
    boxShadow: t.shadowMd,
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
  };

  return (
    <div style={card}>
      <StatusBar live={live} sessionId={session?.sessionId ?? null} />
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 14px", borderBottom: `1px solid ${t.n7}`, background: t.n9 }}>
        <Icon name="lock" size={13} stroke={1.8} color={driving ? t.green : t.n3} />
        <input
          value={urlDraft}
          onChange={(e) => setUrlDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") go();
            e.stopPropagation();
          }}
          placeholder="https://…"
          style={{
            flex: 1, height: 32, padding: "0 12px", background: t.n85, border: `1px solid ${t.n7}`,
            borderRadius: t.radius2xl, fontFamily: t.fontMono, fontSize: "0.78rem", color: t.n1, outline: "none",
          }}
        />
        <Pill onClick={go} disabled={!driving}>Go</Pill>
        <Pill onClick={() => sid && void refreshInfo(sid)} disabled={!sid}>
          <Icon name="reload" size={13} />
        </Pill>
      </div>

      <div ref={stageRef} style={{ position: "relative", flex: 1, minHeight: 0, display: "flex", alignItems: "center", justifyContent: "center", background: t.n85, padding: 8 }}>
        {session ? (
          <div
            ref={surfaceRef}
            tabIndex={0}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onPointerDown={(e) => void onPointerDown(e)}
            onPointerMove={onPointerMove}
            onKeyDown={onKeyDown}
            style={{
              position: "relative",
              width: fit.w || "100%",
              height: fit.h || "100%",
              background: t.n0,
              borderRadius: 4,
              overflow: "hidden",
              outline: focused && driving ? `2px solid ${t.primary6}` : `1px solid ${t.n7}`,
              cursor: driving ? "crosshair" : "not-allowed",
              touchAction: "none",
            }}
          >
            {/* The stream writes here directly — see the note on frames above. */}
            <img ref={imgRef} alt="live browser" style={{ display: "block", width: "100%", height: "100%" }} />
            {live.status !== "live" && <Blocker live={live} onRetry={() => sockRef.current?.retry()} />}
            {live.status === "live" && !live.controller && <ReadOnlyRibbon />}
            {driving && !focused && (
              <div style={{ position: "absolute", left: 10, bottom: 10, padding: "5px 10px", borderRadius: t.radiusLg, background: `color-mix(in srgb, ${t.n0} 72%, transparent)`, color: t.n9, fontSize: "0.72rem", fontWeight: weight.semibold }}>
                Click the page to send keystrokes
              </div>
            )}
          </div>
        ) : (
          <Empty startUrl={startUrl} opening={opening} error={openError} onOpen={() => void open()} />
        )}
      </div>

      <InputBar live={live} recording={!!attemptId} onRetry={() => sockRef.current?.retry()} onStop={() => sockRef.current?.disconnect()} />
    </div>
  );
}

// --------------------------------------------------------------------------- chrome

const STATUS_COPY: Record<LiveState["status"], string> = {
  idle: "Not connected",
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting",
  closed: "Disconnected",
};

function statusColor(live: LiveState): string {
  if (live.status === "live") return live.controller ? t.green : t.yellow;
  if (live.status === "connecting") return t.primary6;
  if (live.status === "reconnecting") return t.yellow;
  return t.red;
}

function StatusBar({ live, sessionId }: { live: LiveState; sessionId: string | null }) {
  const color = statusColor(live);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 14px", background: t.n8, borderBottom: `1px solid ${t.n7}` }}>
      <span style={{ width: 9, height: 9, borderRadius: t.radiusFull, background: color, flexShrink: 0 }} />
      <span style={{ fontSize: "0.78rem", fontWeight: weight.bold, color: t.n0 }}>
        {STATUS_COPY[live.status]}
        {live.status === "reconnecting" && live.attempt > 0 ? ` · attempt ${live.attempt}` : ""}
      </span>
      <span
        style={{
          fontSize: "0.6875rem", fontWeight: weight.bold, textTransform: "uppercase", letterSpacing: "0.05em",
          padding: "4px 9px", borderRadius: t.radiusMd,
          color: live.controller ? t.greenDark : t.yellowDark,
          background: `color-mix(in srgb, ${live.controller ? t.green : t.yellow} 14%, transparent)`,
        }}
      >
        {live.controller ? "Driving" : "Read-only"}
      </span>
      <span style={{ flex: 1 }} />
      <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", color: t.n3 }}>
        {sessionId ? `${sessionId} · ${live.viewport.width}×${live.viewport.height}` : "no session"}
      </span>
    </div>
  );
}

/** A stream that is not live blocks the surface outright. A subtle badge would
 *  let an annotator keep clicking into nothing. */
function Blocker({ live, onRetry }: { live: LiveState; onRetry: () => void }) {
  const terminal = live.status === "closed" || live.status === "idle";
  return (
    <div
      style={{
        position: "absolute", inset: 0, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", gap: 10, textAlign: "center", padding: 24,
        background: `color-mix(in srgb, ${t.n0} 66%, transparent)`, color: t.n9,
      }}
    >
      <Icon name="alert" size={26} stroke={1.7} color={terminal ? t.red : t.yellow} />
      <div style={{ fontSize: "0.95rem", fontWeight: weight.bold }}>
        {STATUS_COPY[live.status]}
        {live.status === "reconnecting" && live.attempt > 0 ? ` · attempt ${live.attempt}` : ""}
      </div>
      <div style={{ fontSize: "0.8125rem", maxWidth: 420, opacity: 0.85 }}>
        {live.detail ?? "The live browser stream is not connected. Input is not being delivered."}
      </div>
      {terminal && (
        <span
          onClick={onRetry}
          style={{
            marginTop: 4, padding: "6px 14px", borderRadius: t.radiusLg, cursor: "pointer",
            background: t.primary6, color: t.n9, fontSize: "0.8125rem", fontWeight: weight.semibold,
          }}
        >
          Reconnect
        </span>
      )}
    </div>
  );
}

function ReadOnlyRibbon() {
  return (
    <div
      style={{
        position: "absolute", top: 10, left: "50%", transform: "translateX(-50%)",
        padding: "5px 12px", borderRadius: t.radiusPill, whiteSpace: "nowrap",
        background: `color-mix(in srgb, ${t.yellow} 88%, transparent)`, color: t.n0,
        fontSize: "0.72rem", fontWeight: weight.bold,
      }}
    >
      Read-only — another connection is driving this browser
    </div>
  );
}

/** Sent / pending / unacked / refused, always visible. Input that was refused
 *  and input that was dispatched into a socket that then died are different
 *  facts, and only the annotator can decide what to do about either. */
function InputBar({ live, recording, onRetry, onStop }: { live: LiveState; recording: boolean; onRetry: () => void; onStop: () => void }) {
  const stat = (label: string, value: number, tone: string) => (
    <span style={{ fontFamily: t.fontMono, fontSize: "0.6875rem", color: tone }}>
      {label} {value}
    </span>
  );
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "10px 16px", borderTop: `1px solid ${t.n7}`, background: t.n9, flexShrink: 0 }}>
      {stat("sent", live.lastInputId, t.n2)}
      {stat("pending", live.pendingInputs, live.pendingInputs ? t.n1 : t.n3)}
      {stat("unacked", live.unackedInputs, live.unackedInputs ? t.redDark : t.n3)}
      {stat("refused", live.droppedInputs, live.droppedInputs ? t.redDark : t.n3)}
      <span style={{ flex: 1, minWidth: 0, fontSize: "0.72rem", color: live.detail ? t.redDark : t.n3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {live.detail ?? (recording ? "Recording interactions" : "Not recording — no session")}
      </span>
      <Pill onClick={onRetry} disabled={live.status === "live"}>Reconnect</Pill>
      <Pill onClick={onStop} disabled={live.status === "closed" || live.status === "idle"}>Stop</Pill>
    </div>
  );
}

function Empty({ startUrl, opening, error, onOpen }: { startUrl?: string; opening: boolean; error: string | null; onOpen: () => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, textAlign: "center", padding: 24, color: t.n2 }}>
      <Icon name="play" size={22} color={t.n3} />
      <div style={{ fontSize: "0.9375rem", fontWeight: weight.bold, color: t.n0 }}>No live browser attached</div>
      <div style={{ fontSize: "0.8125rem", maxWidth: 420 }}>
        {error ?? (startUrl ? "Open a browser session to watch and drive the same page the agent uses." : "This pane needs a live session; none was provided.")}
      </div>
      {startUrl && (
        <span
          onClick={opening ? undefined : onOpen}
          style={{
            marginTop: 4, padding: "6px 14px", borderRadius: t.radiusLg, cursor: opening ? "default" : "pointer",
            background: opening ? t.n6 : t.primary6, color: t.n9, fontSize: "0.8125rem", fontWeight: weight.semibold,
          }}
        >
          {opening ? "Opening…" : "Start live browser"}
        </span>
      )}
    </div>
  );
}

function Pill({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <span
      onClick={disabled ? undefined : onClick}
      style={{
        display: "inline-flex", alignItems: "center", gap: 6, padding: "5px 11px", borderRadius: t.radiusLg,
        border: `1px solid ${t.n6}`, background: t.n9, color: disabled ? t.n4 : t.primary6,
        fontSize: "0.75rem", fontWeight: weight.semibold, cursor: disabled ? "default" : "pointer", whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}
