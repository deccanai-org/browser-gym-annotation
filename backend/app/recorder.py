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
) -> models.InteractionEvent:
    """Append one raw event. Never mutates or removes anything already recorded."""
    payload = dict(payload or {})
    if kind in ("key", "type", "fill") and _is_sensitive(target):
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
    )
    db.add(ev)
    db.flush()
    return ev


# --------------------------------------------------------------------------- boundaries
# A press/release pair separated by more than this is a drag or a long-press, not
# one click; consecutive keystrokes further apart than this are separate edits.
CLICK_PAIR_MS = 700
KEY_COALESCE_MS = 1500


def _same_target(a: dict | None, b: dict | None) -> bool:
    a, b = a or {}, b or {}
    for k in ("testId", "id", "selector"):
        if a.get(k) and a.get(k) == b.get(k):
            return True
    return bool(a) and a == b


def coalesce(events: Iterable[models.InteractionEvent | dict]) -> list[dict]:
    """Fold a raw event stream into candidate ACTIONS.

    * ``mousePressed`` + ``mouseReleased`` on the same target within CLICK_PAIR_MS
      become one ``click``
    * consecutive ``key`` events on one field become a single ``fill`` carrying the
      final value (a trajectory should say "type the answer", not replay 12 keys)
    * scrolls marked ``auto`` are dropped: a page scrolling itself is not a human
      action, and replaying it would fight the page
    """
    raw = [e if isinstance(e, dict) else _as_dict(e) for e in events]
    out: list[dict] = []
    i = 0
    while i < len(raw):
        e = raw[i]
        kind = e.get("kind")

        if kind == "mousePressed":
            j = i + 1
            if (
                j < len(raw)
                and raw[j].get("kind") == "mouseReleased"
                and _same_target(e.get("target"), raw[j].get("target"))
                and abs(int(raw[j].get("t", 0)) - int(e.get("t", 0))) <= CLICK_PAIR_MS
            ):
                out.append({**e, "kind": "click", "sources": [e.get("seq"), raw[j].get("seq")]})
                i = j + 1
                continue
            out.append({**e, "kind": "press", "sources": [e.get("seq")]})
            i += 1
            continue

        if kind == "key":
            group = [e]
            j = i + 1
            while (
                j < len(raw)
                and raw[j].get("kind") == "key"
                and _same_target(e.get("target"), raw[j].get("target"))
                and abs(int(raw[j].get("t", 0)) - int(raw[j - 1].get("t", 0))) <= KEY_COALESCE_MS
            ):
                group.append(raw[j])
                j += 1
            last = group[-1]
            value = last.get("payload", {}).get("value")
            if value is None:  # no field value reported — fall back to the typed text
                value = "".join(str(g.get("payload", {}).get("text", "")) for g in group)
            redacted = any(g.get("payload", {}).get("redacted") for g in group)
            out.append({
                **last, "kind": "fill",
                "payload": {"value": _REDACTED if redacted else value, "redacted": redacted},
                "sources": [g.get("seq") for g in group],
            })
            i = j
            continue

        if kind == "scroll" and e.get("payload", {}).get("auto"):
            i += 1  # the page scrolled itself; not a human action
            continue

        out.append({**e, "sources": [e.get("seq")]})
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
