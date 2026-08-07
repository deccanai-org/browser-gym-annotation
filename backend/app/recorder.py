"""Recorder — raw browser interactions in, committed replayable steps out.

Two entities, deliberately separate (§3.2):

* ``InteractionEvent`` — append-only RAW events. Exploration lives here and never
  pollutes the golden. This is the AUTHORITATIVE action source: the gym's
  route-level ``log_action`` records backend activity, which misses navigation,
  scrolling, dropdown opening, typing before submit, focus changes and any click
  that mutates nothing — and one browser action can produce zero, one or several
  backend entries, with no way to correlate them.
* ``TrajectoryStep`` — the normalized, replayable action a human chose to COMMIT.

Action boundaries (§8.6) are the crux: a stream of raw events is not a trajectory.
Keystrokes coalesce into one ``fill``; a press/release pair is one ``click``;
target evidence is captured BEFORE dispatch (afterwards the element may be gone);
and sensitive inputs are redacted at record time, never later.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models

# Fields whose VALUES must never be persisted. Redaction happens at record time —
# once a secret is in the append-only log, "delete it later" is not a real option.
_SENSITIVE = re.compile(
    r"(pass|pwd|secret|token|otp|cvv|cvc|ccv|card|cardnum|creditcard|ssn|social|pin|auth)",
    re.I,
)
_REDACTED = "«redacted»"


def _is_sensitive(target: dict | None) -> bool:
    """Whether this element's typed value must never be persisted.

    An EMPTY target counts as sensitive. A keystroke whose field we cannot name
    is a keystroke we cannot clear, and the asymmetry is stark: a wrong redaction
    costs one recoverable search query, while a missed one writes a password into
    an append-only log forever. The client is expected to name the focused
    element (the live browser exposes /focused for exactly this), so an unnamed
    one means something went wrong — which is precisely when guessing is worst.
    """
    if target is None or not target:
        return True
    hay = " ".join(
        str(target.get(k, "")) for k in ("name", "id", "testId", "role", "label", "placeholder", "type", "autocomplete")
    )
    if str(target.get("type", "")).lower() == "password":
        return True
    return bool(_SENSITIVE.search(hay))


def _next_seq(db: Session, attempt_id: UUID) -> int:
    """Sequence is per attempt and monotonic, so raw order is reconstructable even
    when events arrive out of order."""
    cur = db.scalar(
        select(func.max(models.InteractionEvent.seq)).where(models.InteractionEvent.attempt_id == attempt_id)
    )
    return int(cur or 0) + 1


def record_event(
    db: Session,
    *,
    attempt_id: UUID,
    kind: str,
    payload: dict | None = None,
    target: dict | None = None,
    url: str = "",
    tab: str = "",
    actor: str = "human",
    workspace_lease_id: UUID | None = None,
    client_event_id: str | None = None,
) -> models.InteractionEvent | None:
    """Append one raw event. Never mutates or removes anything already recorded.

    Returns None when `client_event_id` has already been recorded for this
    attempt — the client re-queues a whole batch on any failure, including a
    network drop AFTER we committed it, so a retry must be a no-op rather than a
    second copy of every event in it.
    """
    if client_event_id:
        seen = db.scalar(
            select(models.InteractionEvent.id).where(
                models.InteractionEvent.attempt_id == attempt_id,
                models.InteractionEvent.client_event_id == client_event_id,
            )
        )
        if seen is not None:
            return None
    payload = dict(payload or {})
    # EVERY kind that can carry typed text. Missing one writes a password into an
    # append-only log forever, so this list must grow with the client's vocabulary
    # — `keyChar`/`keyPress`/`paste` are the current names, the rest are legacy.
    if kind in ("key", "keyChar", "keyPress", "type", "fill", "paste") and _is_sensitive(target):
        if "text" in payload:
            payload["text"] = _REDACTED
        if "value" in payload:
            payload["value"] = _REDACTED
        payload["redacted"] = True

    ev = models.InteractionEvent(
        attempt_id=attempt_id,
        workspace_lease_id=workspace_lease_id,
        seq=_next_seq(db, attempt_id),
        kind=kind,
        actor=actor,
        payload=payload,
        target=target or {},
        url=url,
        tab=tab,
        client_event_id=client_event_id,
    )
    db.add(ev)
    db.flush()
    return ev


# --------------------------------------------------------------------------- boundaries
# A press/release pair separated by more than this is a drag or a long-press, not
# one click; consecutive keystrokes further apart than this are separate edits.
CLICK_PAIR_MS = 700
KEY_COALESCE_MS = 1500


# A press that moves further than this is a drag, not a click.
DRAG_PX = 0.012          # ~15px on a 1280-wide viewport, in normalized units
SCROLL_COALESCE_MS = 600
SCROLL_MIN_DY = 40       # below this a "scroll" is trackpad jitter

# Scroll is viewport motion, not a state change. The trajectory records what the
# annotator did to the WORLD, and a scroll changes nothing a verifier can read
# and nothing an SFT target should learn to imitate — it also varies with screen
# size and zoom, so it is not even reproducible across annotators. Every browser-
# gym paper surveyed treats scrolling as a byproduct of navigation and none of
# them verify against it.
#
# The RAW InteractionEvent rows are untouched by this: only the promotion to a
# STEP is suppressed, so the audit trail survives and flipping this back plus
# re-materializing from seq 0 on a fresh version restores the scroll steps.
SCROLL_IS_A_STEP = False

# Things the ENVIRONMENT did on its own — a popup the page opened, a redirect, an
# effect the backend produced. They arrive on the same event channel as real
# actions (LiveBrowserPane's `onNotice` pushes them), and `coalesce` used to end
# in a catch-all that turned every unrecognised kind into a step.
#
# Deliberately a DENY-list, not an allow-list: an allow-list silently drops any
# action kind added later, and losing a real action is far worse than keeping a
# stray one. Add to this set only after confirming the kind is not something an
# annotator can do.
ENVIRONMENT_KINDS = frozenset({"popup", "notice", "redirect", "backend_effect"})

# The executor's ENTIRE vocabulary — `act()` in live_browser/service.py. Anything
# else comes back {"ok": false, "error": "unsupported action …"}, which certify
# writes down as `diverged`, so a step outside this set can never ship however
# faithful it is. Two rules follow, and both are applied below:
#
# * where a recorded gesture HAS an executor kind, emit that kind (`keyPress` ->
#   `press`) — pressing Enter to submit a search is one of the commonest browser
#   actions there is, and it used to produce a step nothing could replay;
# * where it has NONE, give it its own name rather than the name of a different
#   action. A right-click recorded as `click` replays as a LEFT click — `act()`
#   takes no button argument at all — and certify then stamps it verified.
EXECUTOR_KINDS = frozenset({
    "click", "submit", "fill", "type", "select", "select_option", "check",
    "press", "navigate", "open_tab", "switch_tab", "close_tab", "scroll", "wait",
    # The executor performs these as the real gesture, with no JS fallback (see
    # live_browser/service.py `act`). They were outside this set while it could
    # only left-click, which was right then and wrong the moment it learned how:
    # a triple-click in a text field folds to a `dblclick`, and every trajectory
    # containing one was refused at certify as unreplayable.
    "right_click", "dblclick",
})

# Gestures the executor still cannot perform: `drag` and `long_press`. They are
# deliberately outside EXECUTOR_KINDS so they fail LOUDLY at certify and are
# marked when they are folded (see materialize), rather than replaying as a
# different action — a drag that replays as a click is a lie the transcript
# cannot show.
_BUTTON_KINDS = {"right": "right_click", "middle": "middle_click"}

# Keys that only EDIT the value of the field being typed into, so they belong
# inside a fill rather than splitting it. The final value comes from the page, so
# their effect is already accounted for.
_EDIT_KEYS = {"Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
              "Home", "End", "Shift"}
# Keys that END an edit: they commit, move focus, or abandon it.
_TERMINATOR_KEYS = {"Enter", "Tab", "Escape"}

# The client renamed two raw kinds so a mouse press and a non-printable key stop
# sharing one name (they used to both fold to `press`, and the executor turned
# that into "press Enter" — so a long-press committed as an Enter keystroke).
_ALIASES = {"mousePressed": "mouseDown", "mouseReleased": "mouseUp", "key": "keyChar"}


def _kind(e: dict) -> str:
    k = str(e.get("kind") or "")
    return _ALIASES.get(k, k)


def _same_target(a: dict | None, b: dict | None) -> bool:
    """Whether two events name the SAME element.

    `targetKey` first: the live service derives one deterministic identity per
    element, so this no longer depends on which call observed it. Dict equality
    is deliberately NOT a fallback — `describe` carries `text` and `focused` does
    not, so the same field compared unequal the moment focus moved by Tab, and a
    single typing run split into two fills. And `{} == {}` was True, which made
    every unnamed target "the same element" as every other.
    """
    a, b = a or {}, b or {}
    ka, kb = a.get("targetKey"), b.get("targetKey")
    if ka and kb:
        return ka == kb
    for k in ("testId", "id", "name", "selector"):
        if a.get(k) and a.get(k) == b.get(k):
            return True
    return False


def _click_kind(button: str, clicks: int) -> str:
    """What a completed press/release actually was.

    The button decides first: a right-click is a right-click whether it landed
    once or twice, and it is the fact the executor cannot honour.
    """
    return _BUTTON_KINDS.get(button) or ("dblclick" if clicks >= 2 else "click")


def _dist(a: dict, b: dict) -> float:
    pa, pb = a.get("payload") or {}, b.get("payload") or {}
    try:
        return max(abs(float(pb.get("nx", 0)) - float(pa.get("nx", 0))),
                   abs(float(pb.get("ny", 0)) - float(pa.get("ny", 0))))
    except (TypeError, ValueError):
        return 0.0


def coalesce(events: Iterable[models.InteractionEvent | dict]) -> list[dict]:
    """Fold a raw event stream into candidate ACTIONS.

    * ``mousePressed`` + ``mouseReleased`` on the same target within CLICK_PAIR_MS
      become one ``click`` — or ``right_click``/``middle_click``, which the
      executor has no way to perform and must therefore not be called ``click``
    * consecutive ``key`` events on one field become a single ``fill`` carrying the
      final value (a trajectory should say "type the answer", not replay 12 keys)
    * a ``keyPress`` that is not part of an edit becomes ``press``, the executor's
      own name for it
    * scrolls marked ``auto`` are dropped: a page scrolling itself is not a human
      action, and replaying it would fight the page

    The kinds that come out are the executor's (EXECUTOR_KINDS) wherever one
    exists; the rest are named for the gesture they were.
    """
    raw = [e if isinstance(e, dict) else _as_dict(e) for e in events]
    out: list[dict] = []
    i = 0
    while i < len(raw):
        e = raw[i]
        kind = _kind(e)

        # --- pointer: click / dblclick / drag / long_press ---------------------
        if kind == "mouseDown":
            j = i + 1
            if j < len(raw) and _kind(raw[j]) == "mouseUp":
                up = raw[j]
                dt = abs(int(up.get("t", 0)) - int(e.get("t", 0)))
                moved = _dist(e, up)
                same = _same_target(e.get("target"), up.get("target"))
                clicks = int((up.get("payload") or {}).get("clicks", 1) or 1)
                button = str((e.get("payload") or {}).get("button", "left"))
                # A drag REQUIRES movement. `not same` alone used to be enough,
                # and `_same_target` answers False for two unidentified targets
                # deliberately (see its docstring) — so every click whose element
                # the page could not name became a "drag": description "drag", no
                # locator, not replayable, and a lie about what the annotator did.
                # Measured on the real corpus: 14 of 90 recorded human steps, all
                # 14 with an empty locator and 13 of 14 with the pointer not
                # having moved by a single pixel. Target identity still promotes
                # a SHORT drag between two named elements, which is why it stays.
                if moved > DRAG_PX or (not same and moved > 0 and (e.get("target") or up.get("target"))):
                    # A press that travelled is a DRAG. Recording it as a click at
                    # the start point (what synthesising both ends produced) both
                    # loses the gesture and replays as the wrong action.
                    act = {**up, "kind": "drag", "payload": {
                        **(up.get("payload") or {}),
                        "from": {"nx": (e.get("payload") or {}).get("nx"),
                                 "ny": (e.get("payload") or {}).get("ny")},
                        "fromLocator": e.get("target") or {},
                        "button": button,
                    }}
                elif dt > CLICK_PAIR_MS:
                    act = {**e, "kind": "long_press"}
                else:
                    act = {**e, "kind": _click_kind(button, clicks),
                           "payload": {**(e.get("payload") or {}), "button": button,
                                       "clicks": clicks},
                           "url": up.get("url") or e.get("url")}
                out.append({**act, "sources": [e.get("seq"), up.get("seq")]})
                i = j + 1
                continue
            # An unpaired press. Dropped rather than guessed: it used to become
            # `press`, which the executor turns into "press Enter".
            out.append({**e, "kind": "press_incomplete", "incomplete": True,
                        "sources": [e.get("seq")]})
            i += 1
            continue

        if kind == "mouseUp":       # its down was lost; nothing to fold
            i += 1
            continue

        # --- typing: one fill per edit ---------------------------------------
        if kind in ("keyChar", "paste"):
            group = [e]
            j = i + 1
            while j < len(raw):
                nk = _kind(raw[j])
                if not _same_target(e.get("target"), raw[j].get("target")):
                    break
                if abs(int(raw[j].get("t", 0)) - int(raw[j - 1].get("t", 0))) > KEY_COALESCE_MS:
                    break
                if nk in ("keyChar", "paste"):
                    group.append(raw[j]); j += 1; continue
                # An editing key changes the value but does not end the edit; the
                # value we take is the page's, so its effect is already included.
                # This is the Backspace fix: "m,u,g,⌫,s" is ONE fill of "mus",
                # not fill("mug") + press(Backspace) + fill("s") — which replayed
                # as "s", because fill REPLACES.
                if nk == "keyPress" and str((raw[j].get("payload") or {}).get("key")) in _EDIT_KEYS:
                    group.append(raw[j]); j += 1; continue
                break
            last = group[-1]
            value = (last.get("payload") or {}).get("value")
            redacted = any((g.get("payload") or {}).get("redacted") for g in group)
            act = {**last, "kind": "fill", "sources": [g.get("seq") for g in group]}
            if redacted:
                # Never emit the placeholder as a literal value: it used to be
                # TYPED into the field and still pass the replay gate, shipping a
                # golden that types "«redacted»" into a password box.
                act["payload"] = {"value": None, "redacted": True}
                act["needsValue"] = True
            elif value is None:
                # No page-reported value (an unacked keystroke). Concatenating the
                # characters we sent would be a plausible LIE — say so instead.
                act["payload"] = {"value": None, "redacted": False}
                act["incomplete"] = True
            else:
                act["payload"] = {"value": value, "redacted": False}
            out.append(act)
            i = j
            continue

        # --- a key that is not part of an edit ---------------------------------
        if kind == "keyPress":
            # `press` is the executor's name for this, and the ONLY name it
            # answers to. Emitting the client's `keyPress` meant Enter-to-submit
            # — the commonest browser action there is, and the one that ends most
            # search steps — produced a step certify could only ever call
            # diverged, which blocks the whole trajectory from shipping.
            #
            # Modifiers ride in the key itself: `act()` passes only `args.key` to
            # the browser, which parses "Meta+v" — so a copy/paste or a select-all
            # replays intact rather than as a bare "v" typed into the field.
            payload = e.get("payload") or {}
            key = str(payload.get("key") or "")
            mods = [str(m) for m in (payload.get("modifiers") or []) if m]
            # A press with no key is NOT translated: `act()` defaults a missing
            # key to Enter, so it would submit whatever form was open.
            out.append({**e, "kind": "press" if key else kind, "sources": [e.get("seq")],
                        "payload": {**payload, "key": "+".join([*mods, key]) if mods else key}})
            i += 1
            continue

        # --- scroll ------------------------------------------------------------
        if kind == "scroll":
            if (e.get("payload") or {}).get("auto"):
                i += 1              # the page scrolled itself; not a human action
                continue
            group = [e]
            j = i + 1
            while (j < len(raw) and _kind(raw[j]) == "scroll"
                   and not (raw[j].get("payload") or {}).get("auto")
                   and raw[j].get("tab") == e.get("tab")
                   and abs(int(raw[j].get("t", 0)) - int(raw[j - 1].get("t", 0))) <= SCROLL_COALESCE_MS):
                group.append(raw[j]); j += 1
            dy = sum(float((g.get("payload") or {}).get("dy", 0) or 0) for g in group)
            dx = sum(float((g.get("payload") or {}).get("dx", 0) or 0) for g in group)
            i = j
            # One flick of a wheel is ~20 ticks and ONE human intent ("scroll down
            # to the reviews"); 20 steps would drown the trajectory.
            if abs(dy) < SCROLL_MIN_DY and abs(dx) < SCROLL_MIN_DY:
                continue
            if not SCROLL_IS_A_STEP:
                # Consumed, not emitted. The events are still folded (so the
                # watermark advances and they are never re-read) — they simply do
                # not become a step. Gated HERE rather than in materialize because
                # `coalesce` also backs GET /sessions/{id}/actions and the legacy
                # commit path, so one gate covers every downstream reader.
                continue
            last = group[-1]
            out.append({**last, "kind": "scroll",
                        "payload": {**(last.get("payload") or {}), "dy": dy, "dx": dx},
                        "sources": [g.get("seq") for g in group]})
            continue

        if kind in ENVIRONMENT_KINDS:
            # Something the ENVIRONMENT did, not something the annotator did.
            # Consumed like a suppressed scroll: the raw event is kept and the
            # watermark advances, it simply does not become a step.
            #
            # The catch-all below promoted these, so a trajectory carried steps
            # reading `popup` with no target and no locator — 15 of 90 recorded
            # human steps. In a shipped sample that teaches a policy to emit
            # "popup" as an action, which is not an action anyone can take.
            i += 1
            continue

        out.append({**e, "kind": kind, "sources": [e.get("seq")]})
        i += 1
    return out


def _as_dict(ev: models.InteractionEvent) -> dict:
    return {
        "seq": ev.seq, "kind": ev.kind, "actor": ev.actor, "payload": ev.payload or {},
        "target": ev.target or {}, "url": ev.url, "tab": ev.tab,
        "t": int((ev.payload or {}).get("t", 0)),
    }


def candidate_actions(db: Session, attempt_id: UUID) -> list[dict]:
    """Everything recorded for this attempt, folded into candidate actions. The
    human picks from these; nothing is committed automatically."""
    events = db.scalars(
        select(models.InteractionEvent)
        .where(models.InteractionEvent.attempt_id == attempt_id)
        .order_by(models.InteractionEvent.seq)
    ).all()
    return coalesce(events)


def semantic_locator(target: dict | None) -> dict:
    """A durable way to find the element again, best first. Coordinates are the
    LAST resort: they break the moment the layout shifts."""
    t = target or {}
    loc: dict[str, Any] = {}
    if t.get("testId"):
        loc["testId"] = t["testId"]
    if t.get("role"):
        loc["role"] = t["role"]
    if t.get("name") or t.get("label"):
        loc["name"] = t.get("name") or t.get("label")
    if t.get("id"):
        loc["id"] = t["id"]
    if t.get("selector"):
        loc["css"] = t["selector"]
    if t.get("text"):
        loc["text"] = str(t["text"])[:120]
    return loc


# A gym action records its target as a raw selector — "[data-test-id='btn-send']",
# "#send", ".cart a". A committed step needs the semantic form, because that is
# what the replayer resolves and what survives a layout change.
_TEST_ID = re.compile(r"""\[data-test-id=['"]([^'"]+)['"]\]""")


def locator_from_selector(selector: str) -> dict:
    """Turn a recorded CSS selector into the semantic locator a replay speaks.

    Agent runs carry `action_args.selector`, not a locator. Persisting the args
    alone leaves the step unreplayable, and finalization refuses it — so an
    agent-assisted correction could be created, selected and approved and then
    could never actually ship. Preferring the test id keeps the durable handle
    rather than the brittle path it happened to be written as.
    """
    sel = (selector or "").strip()
    if not sel:
        return {}
    match = _TEST_ID.search(sel)
    if match:
        return {"testId": match.group(1)}
    if sel.startswith("#") and " " not in sel:
        return {"id": sel[1:]}
    return {"css": sel}
