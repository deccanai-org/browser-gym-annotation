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
import { FrameRecorder } from "../../lib/liveBrowser";
import { AppTabs } from "./AppTabs";
import type { LiveApp } from "./liveSessionApi";

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

/** Which app the streamed page belongs to, matched by origin. Lets the tab strip
 *  highlight the app actually on screen before the annotator has switched tabs —
 *  the landing app is the task's primary, which is not always apps[0]. */
function appForUrl(apps: LiveApp[], url: string): string | undefined {
  if (!url) return undefined;
  const originOf = (u: string) => { try { return new URL(u).origin; } catch { return ""; } };
  const here = originOf(url);
  if (!here) return undefined;
  return apps.find((a) => originOf(a.url) === here)?.app;
}

export function LiveBrowserPane({
  attemptId,
  session: sessionProp,
  startUrl,
  owner,
  base,
  control = true,
  onSession,
  apps,
  onDropped,
}: {
  /** Interactions the recorder had to DROP. Not cosmetic: from that point the
   *  trajectory is incomplete, and the annotator is the only one who can decide
   *  whether to redo the task. The counter and its alert already existed; only
   *  this wire was missing, so the alert could never fire. */
  onDropped?: (n: number) => void;
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
  /** The realistic gym's apps, when this attempt is a cua-hub one. Renders the
   *  tab strip; a multi-app task is undoable without it. */
  apps?: LiveApp[] | null;
}) {
  const [ownSession, setOwnSession] = useState<OpenedSession | null>(null);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState>(idleLiveState(sessionProp?.viewport ?? DEFAULT_VIEWPORT));
  const [pageUrl, setPageUrl] = useState("");
  const [urlDraft, setUrlDraft] = useState("");
  const [focused, setFocused] = useState(false);
  const [fit, setFit] = useState({ w: 0, h: 0 });
  const [activeApp, setActiveApp] = useState<string | undefined>(undefined);
  const [, setActiveTabId] = useState<string>("");
  // The open press, so its "up" can be paired into a click — or recognised as a drag.
  const downRef = useRef<{ p: NormPoint; at: number; target: Record<string, unknown>; button: string } | null>(null);
  const clickCountRef = useRef(1);
  // Newest frame, kept so a step can carry the pixels the annotator actually saw.
  const lastFrameRef = useRef<string>("");
  const framesRef = useRef<FrameRecorder | null>(null);
  // Screenshots ride their own channel; see FrameRecorder. Throttled because a
  // frame per pointer-move would be pure waste.
  const lastShotRef = useRef(0);

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
    const rec = attemptId ? new EventRecorder({ attemptId, onDrop: (n) => onDropped?.(n) }) : null;
    recRef.current = rec;
    framesRef.current = attemptId ? new FrameRecorder({ attemptId }) : null;
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
        lastFrameRef.current = f.data;
      },
      // The ONLY path from an interaction to the trajectory. Recording here
      // rather than at send-time means nothing is recorded that did not apply,
      // and every event carries the state the service says it produced.
      onRecord: (ev) => {
        const id = recRef.current?.push(ev);
        // The pixels at the moment of the action. Keyed to the event's own id, so
        // the backend can hang the screenshot on the step that event became —
        // whichever of the two arrives first.
        if (id) captureFrame(id);
      },
      onNotice: (n) => {
        // A popup or redirect the page did on its own — no ack carries it.
        if (n.url) setPageUrl(String(n.url));
        recRef.current?.push({
          kind: String(n.event ?? "notice"),
          payload: n as Record<string, unknown>,
          url: String(n.url ?? ""),
          tab: String(n.tabId ?? ""),
        });
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
      const tgt = targetRef.current;
      sock.send({ type: "scroll", nx: p.nx, ny: p.ny, dy, dx }, (st) => ({
        kind: "scroll",
        payload: { dy, dx, nx: p.nx, ny: p.ny, auto: false, t: Date.now() },
        target: tgt,
        url: st?.url ?? pageUrl,
        tab: st?.tabId ?? "",
      }));
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
    // Capture the pointer so a drag that leaves the surface still delivers its
    // "up" here — otherwise the press has no end and folds into a bogus click.
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* not fatal */ }
    // Describe BEFORE dispatching. Afterwards the element may be gone, and a
    // recorded pixel is not replayable — the committed step needs a locator.
    const target = await describeAt(sid, ticket, p, { base });
    targetRef.current = target;
    focusStaleRef.current = false;  // a click IS a focus change, and we just named it
    const button = e.button === 2 ? "right" : e.button === 1 ? "middle" : "left";
    downRef.current = { p, at: Date.now(), target, button };
    sock.send({ type: "mouse", phase: "down", nx: p.nx, ny: p.ny, button }, (st) => ({
      kind: "mouseDown",
      payload: { t: Date.now(), nx: p.nx, ny: p.ny, button },
      target,
      url: st?.url ?? pageUrl,
      tab: st?.tabId ?? "",
    }));
  };

  /** The other half of a press. Sending a REAL up (rather than synthesising one
   *  1ms after the down, which is what this used to do) is the only way a drag
   *  can ever be told apart from a click: the backend folds a down/up pair into a
   *  click only when they share a target and land close together. */
  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const sock = sockRef.current;
    const p = pointAt(e.clientX, e.clientY);
    const down = downRef.current;
    downRef.current = null;
    if (!sock || !p) return;
    const button = down?.button ?? "left";
    const clicks = clickCountRef.current;
    clickCountRef.current = 1;
    sock.send({ type: "mouse", phase: "up", nx: p.nx, ny: p.ny, button, clicks }, (st) => {
      // The URL after a click is how a navigation caused BY that click becomes
      // visible; nothing else reports it.
      if (st?.url && st.url !== pageUrl) setPageUrl(st.url);
      if (st?.tabId) setActiveTabId(st.tabId);
      return {
        kind: "mouseUp",
        payload: { t: Date.now(), nx: p.nx, ny: p.ny, button, clicks,
                   fromNx: down?.p.nx, fromNy: down?.p.ny },
        target: down?.target ?? targetRef.current,
        url: st?.url ?? pageUrl,
        tab: st?.tabId ?? "",
      };
    });
  };

  /** Grab the frame the annotator is looking at, as a JPEG, for one event.
   *
   *  Uses the <img> the stream already paints rather than asking the service for
   *  a fresh capture: this is exactly what they saw when they acted, and it costs
   *  no round trip. Throttled — a picture per pointer-move is pure waste. */
  const captureFrame = (clientEventId: string) => {
    const frames = framesRef.current;
    const img = imgRef.current;
    if (!frames || !img || !img.naturalWidth) return;
    const now = Date.now();
    if (now - lastShotRef.current < 250) return;
    lastShotRef.current = now;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.drawImage(img, 0, 0);
      const data = canvas.toDataURL("image/jpeg", 0.6).split(",")[1] ?? "";
      if (data) frames.push({ clientEventId, jpegBase64: data, width: canvas.width, height: canvas.height });
    } catch {
      /* a lost screenshot never disturbs the interaction stream */
    }
  };

  const onDoubleClick = () => { clickCountRef.current = 2; };

  const onContextMenu = (e: React.MouseEvent<HTMLDivElement>) => {
    // Suppress the annotator's OWN context menu; the right-click belongs to the
    // remote page (pointerdown/up already carry button="right").
    e.preventDefault();
  };

  /** Paste as one atomic change. Typing it character by character would produce a
   *  different event stream — and a different recorded trajectory — from what the
   *  annotator actually did. */
  const onPaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    const sock = sockRef.current;
    if (!sock) return;
    e.preventDefault();
    const text = e.clipboardData.getData("text");
    if (!text) return;
    const tgt = targetRef.current;
    sock.send({ type: "paste", text }, (st) => ({
      kind: "paste",
      payload: { text, value: (st?.focus as Record<string, unknown> | undefined)?.value, t: Date.now() },
      target: (st?.focus as Record<string, unknown> | undefined) ?? tgt,
      url: st?.url ?? pageUrl,
      tab: st?.tabId ?? "",
    }));
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
    if (["Shift", "Alt", "Meta", "Control", "CapsLock"].includes(e.key)) return;
    // Reserve only what the ANNOTATOR needs for their own browser. Everything
    // else — including Cmd/Ctrl+A, +C, +V — belongs to the remote page: this
    // used to return early on any modifier, so a task that needed a shortcut
    // simply could not be done, and the keystroke was never even sent.
    const mods: string[] = [];
    if (e.shiftKey) mods.push("Shift");
    if (e.altKey) mods.push("Alt");
    if (e.ctrlKey) mods.push("Control");
    if (e.metaKey) mods.push("Meta");
    const accel = e.metaKey || e.ctrlKey;
    if (accel && ["r", "t", "w", "n", "q"].includes(e.key.toLowerCase())) return;
    e.preventDefault();
    const printable = e.key.length === 1 && !accel;
    // A keystroke we cannot attribute must not be attributed to the WRONG field.
    // Clear first, then re-read asynchronously: this handler cannot await without
    // dropping the keystroke's ordering, and an empty target is redacted by the
    // backend while a stale one is not.
    if (focusStaleRef.current) {
      targetRef.current = {};
      void syncFocus();
    }
    const tgt = targetRef.current;
    if (printable) {
      // `keyChar` events fold into ONE fill on the backend. Enter must not be one
      // of them, or the committed step types the Enter key into the field.
      sock.send({ type: "type", text: e.key }, (st) => {
        const focus = (st?.focus as Record<string, unknown> | undefined) ?? undefined;
        return {
          kind: "keyChar",
          // `value` is the field's REAL contents after this keystroke, straight
          // from the page. It is what makes a fill correct: the client only knows
          // the characters it sent, so Backspace, autocomplete or a rejected key
          // made its own idea of the value wrong — "mug⌫s" used to commit as "s".
          payload: { text: e.key, value: focus?.value, t: Date.now() },
          target: focus ?? tgt,
          url: st?.url ?? pageUrl,
          tab: st?.tabId ?? "",
        };
      });
      return;
    }
    sock.send({ type: "key", key: e.key, modifiers: mods }, (st) => {
      const focus = (st?.focus as Record<string, unknown> | undefined) ?? undefined;
      if (st?.url && st.url !== pageUrl) setPageUrl(st.url);
      return {
        kind: "keyPress",
        payload: { key: e.key, modifiers: mods, value: focus?.value, t: Date.now() },
        target: focus ?? tgt,
        url: st?.url ?? pageUrl,
        tab: st?.tabId ?? "",
      };
    });
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
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onPointerMove={onPointerMove}
            onDoubleClick={onDoubleClick}
            onContextMenu={onContextMenu}
            onPaste={onPaste}
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

      {apps && apps.length > 0 && (
        <AppTabs
          apps={apps}
          // Before the annotator switches tabs, the highlighted app must be the
          // one actually on screen — the task's PRIMARY app, which is not always
          // apps[0]. Derive it from the streamed page's origin so a mail task
          // does not sit on the ShopGym tab while showing the mailbox.
          activeApp={activeApp ?? appForUrl(apps, pageUrl) ?? apps[0]?.app}
          disabled={live.status !== "live" || !live.controller}
          onSwitch={(a) => {
            // Switch over the socket so the service rebinds the screencast AND
            // the mouse to that tab. Optimistic: the ack carries the tab it
            // actually landed on, and reconciles this if they disagree.
            //
            // RECORDED, like every other action — moving between apps is the
            // whole shape of a cross-app task ("read the order id in mail, then
            // cancel it in shop"), and without a record factory here the
            // trajectory showed the two halves with nothing between them.
            // Recorded on the ACK, so a switch the service refuses (another
            // connection holds control) never becomes a step.
            const ok = sockRef.current?.send(
              { type: "switch_tab", app: a.app, url: a.url },
              (st) => ({
                kind: "switch_tab",
                // The url is the address, and deliberately the only one. This
                // also recorded the app's position in the STRIP as `tabIndex`,
                // which is a different number from the browser tab index: the
                // strip lists every app the task spans, but only visited apps
                // have tabs. Replay addressed by that index, so a switch landed
                // on whichever tab happened to sit at that position — and on
                // tab 0 when the key was missing, which never fails.
                payload: { app: a.app, url: a.url, t: Date.now() },
                target: { app: a.app, title: a.title, targetKey: `app:${a.app}` },
                url: st?.url ?? a.url,
                tab: st?.tabId ?? a.app,
              }),
            ) ?? false;
            if (ok) {
              setActiveApp(a.app);
              setPageUrl(a.url);
            }
            return ok;
          }}
        />
      )}

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
