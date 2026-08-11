"""Recorded interactions become trajectory steps, as the annotator works.

The old model was: explore freely, then hand-pick actions and `/commit` them in
one shot. That had to exist because committing REPLAYS the chosen actions, and
replaying restores a checkpoint into the live environment — which destroys the
working state of whoever is mid-task. So it could only run once, at the end, and
it was all-or-nothing: action 14 of 40 fails, nothing is kept.

This splits the two ideas apart:

    materialize  (here)  append steps continuously; never touches the browser
    certify      (later) prove they replay, in a SCRATCH world, as often as you like

So a step is a record of something that happened, and `replay_state` is a
separate claim about whether it has been proven. Nothing is discarded for failing
to replay; it is marked, and the annotator fixes that step.

Two rules keep this honest:

* **Withhold the open tail.** A fill still being typed must not become a step and
  then a second step for the rest of the word. Only events older than a settle
  window, or followed by a boundary, are folded.
* **Advance the watermark in the same transaction as the steps.** That is the
  idempotency: re-running produces nothing new, so there is no double-click
  hazard because there is no button.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import blobstore, checkpoints, models, recorder, versions, worlddiff

# An event newer than this may still be part of an edit in progress. Comfortably
# above the 1.5s keystroke-coalescing window, so a slow typist's word is not split
# across two steps.
SETTLE_MS = 2000

# Kinds that never become a step on their own: they are folded into one, or they
# are exploration that carries no world change.
_NON_STEP = {"mouseDown", "mouseUp", "mousePressed", "mouseReleased", "keyChar", "move"}

# Actions that move the VIEWPORT or the browser, never the gym world: nothing in
# them reaches the engine. Used twice below — a batch made only of these cannot be
# losing a race with the engine, and it cannot be the cause of a change either.
_BROWSER_ONLY = {"scroll", "switch_tab", "open_tab", "close_tab", "wait"}

# How long to wait before believing "this action changed nothing".
#
# The world is read a few milliseconds after the action acked, which is a race
# with the mock UI's own POST to the engine — and losing it looks EXACTLY like a
# no-op: the world still hashes to what the batch started from. That was written
# down as {"changed": false}, i.e. "your click did nothing", which is a lie an SFT
# target learns from; NULL at least means "not observed" and the UI renders it as
# such. So an apparent no-op is confirmed by a second read rather than believed.
#
# This is off the annotator's interaction path: their clicks go to the browser
# over the websocket, while this runs inside the background POST /events flush —
# a sync endpoint, so the wait sits in a worker thread and not on the event loop.
# It is paid only when the world looks unchanged, and only when the batch could
# have reached the engine at all.
CONFIRM_MS = 150


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _events(db: Session, attempt_id: UUID, after_seq: int) -> list[models.InteractionEvent]:
    return list(db.scalars(
        select(models.InteractionEvent)
        .where(models.InteractionEvent.attempt_id == attempt_id,
               models.InteractionEvent.seq > after_seq)
        .order_by(models.InteractionEvent.seq)
    ))


# Kinds whose action may still be IN PROGRESS, so a fresh one at the tail must be
# withheld. An edit is the real case: more keystrokes are probably coming, and
# folding now would split one `fill` into two. A click, a navigation or a tab
# switch is COMPLETE the moment it acks — waiting SETTLE_MS on those was what
# forced several actions into one observation window and made a per-step state
# delta unobservable. A trailing `mouseDown` still waits: fold it alone and it
# becomes `press_incomplete` with its `mouseUp` orphaned in the next batch.
_OPEN_TAIL = {"keyChar", "keyPress", "key", "paste", "type", "fill",
              "mouseDown", "mousePressed"}


def _settled(rows: list[models.InteractionEvent], now_ms: int) -> list[models.InteractionEvent]:
    """The prefix that is safe to fold — see "withhold the open tail" above."""
    cut = len(rows)
    while cut > 0:
        row = rows[cut - 1]
        if row.kind not in _OPEN_TAIL:
            break
        t = int((row.payload or {}).get("t") or 0)
        if t and now_ms - t < SETTLE_MS:
            cut -= 1
            continue
        break
    return rows[:cut]


def _acted_at(ev: models.InteractionEvent | None) -> datetime | None:
    """WHEN the annotator performed this step — the only per-step timing a
    trajectory carries.

    The client's `payload.t` rather than the row's `occurred_at`, because
    `occurred_at` is the moment the BATCH was written: the recorder flushes on a
    boundary or a 1.2s timer, so a minute of work came back as a handful of
    identical timestamps and no step had a duration. `t` is the annotator's own
    clock, which makes the absolute time only as good as their machine's — but
    the intervals between steps, which is what a trajectory dataset is actually
    read for, are then real.

    Naive UTC, matching every other datetime written here; a tz-aware value in a
    TIMESTAMP WITHOUT TIME ZONE column silently loses its offset.
    """
    if ev is None:
        return None
    t = int((ev.payload or {}).get("t") or 0)
    if t <= 0:
        return ev.occurred_at
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).replace(tzinfo=None)


#: Tag names as a person would say them. `describe` reports `role` as the aria
#: role or, failing that, the tag name — so most of what arrives here is HTML, and
#: "click the a" reads like a typo rather than a link.
_ROLE_WORDS = {
    "a": "link", "button": "button", "input": "field", "textarea": "field",
    "select": "dropdown", "img": "image", "li": "item", "td": "cell",
    "th": "column header", "label": "label", "summary": "disclosure",
}


def _at_phrase(payload: dict) -> str:
    """Where on the page, as a percentage. Only used when there is nothing better."""
    try:
        nx = float(payload["nx"])
        ny = float(payload["ny"])
    except (KeyError, TypeError, ValueError):
        return ""
    return f" at {round(nx * 100)}%, {round(ny * 100)}% of the page"


def _click_phrase(tgt: dict, payload: dict) -> str:
    """What a click says it landed on.

    A bare "click" is the least useful thing this module can emit, and it is what
    every click said whenever the pane's describe round trip came back empty: a
    trajectory of anonymous clicks cannot be reviewed and is worth nothing as
    training data.

    The element's own WORDS come first, because that is what a reviewer can check
    against the screenshot — an annotator recognises "Add to cart", not
    `data-test-id=add-to-cart-42`. Note this is the opposite order from
    `_locator`, deliberately: that one wants the most stable handle, this one wants
    the most recognisable. When there is genuinely no name anywhere, it says so and
    gives the position, rather than inventing one.
    """
    # What the element SAYS is quoted; what it is CALLED internally is not. A
    # reviewer reads `click "Add to cart" button` straight off the screenshot,
    # whereas quoting `btn-cart` only dresses up an identifier as a label.
    said = " ".join(str(tgt.get("text") or "").split())[:40] or str(tgt.get("label") or "").strip()
    ident = str(tgt.get("name") or "").strip() or str(tgt.get("testId") or "").strip()
    role = str(tgt.get("role") or "").strip().lower()
    # An unrecognised long role is somebody's custom aria value; pass a short one
    # through, but never paste a sentence into the description.
    word = _ROLE_WORDS.get(role, role if role and len(role) < 20 else "")
    if said:
        return f'"{said}" {word}'.strip()
    if ident:
        return f"{ident} {word}".strip()
    where = _at_phrase(payload)
    return f"an unnamed {word}{where}" if word else f"an unidentified element{where}"


def _describe(action: dict) -> str:
    """A one-line human summary — what the step list actually shows.

    Reads `target` (what `coalesce` emits), not `locator`: the two are different
    keys and getting it wrong silently degrades every description to a bare verb.
    """
    tgt = action.get("target") or {}
    kind = action.get("kind", "")
    name = (tgt.get("testId") or tgt.get("name") or tgt.get("label")
            or (tgt.get("text") or "")[:40] or tgt.get("role") or "")
    payload = action.get("payload") or {}
    if kind == "fill":
        value = payload.get("value")
        shown = "«redacted»" if payload.get("redacted") else (f'"{value}"' if value is not None else "?")
        return f"fill {name} with {shown}".strip()
    if kind == "navigate":
        return f"navigate to {payload.get('url', '')}".strip()
    if kind == "switch_tab":
        # Without this a cross-app move renders as a bare "switch_tab", which is
        # the one step where WHICH app matters most.
        where = payload.get("app") or tgt.get("app") or payload.get("url") or "another app"
        return f"switch to {where}"
    if kind == "scroll":
        dy = payload.get("dy", 0)
        return f"scroll {'down' if (dy or 0) > 0 else 'up'}" + (f" in {name}" if name else "")
    if kind == "select_text":
        # The TEXT is the substance of this step — it is the value the annotator
        # went and read. "select_text order-total" says nothing they can check.
        #
        # A copy is called a copy: reading a value and carrying it to another app
        # is a different intent from merely highlighting, and on a cross-app task
        # it is usually the pivot the whole trajectory turns on.
        text = str(payload.get("text") or "")[:60]
        verb = "copy" if payload.get("via") == "copy" else "select"
        return f'{verb} "{text}"' + (f" in {name}" if name else "")
    if kind in ("click", "dblclick"):
        verb = "double-click" if kind == "dblclick" else "click"
        return f"{verb} {_click_phrase(tgt, payload)}"
    if kind == "right_click":
        return f"right-click {_click_phrase(tgt, payload)}"
    if kind == "middle_click":
        return f"middle-click {_click_phrase(tgt, payload)}"
    if kind == "press":
        # "press Enter", not "press input-search": the KEY is the action here, and
        # the field is only where it landed.
        key = payload.get("key") or "?"
        return f"press {key}" + (f" in {name}" if name else "")
    return f"{kind} {name}".strip() if name else kind


def should_materialize(attempt: models.ReviewSession) -> bool:
    """Only a HUMAN-DO attempt folds its interactions into steps automatically.

    In the agent-review flow the trajectory is the agent's run, and the annotator's
    own clicking is exploration that must never pollute it — that separation is
    the whole reason raw events and steps are different tables. In the human-do
    flow the annotator IS the author, so their actions ARE the trajectory; the
    golden is protected downstream instead, by certification and by the steps they
    delete.
    """
    return getattr(attempt, "mode", "") == "human_do"


def _chain_and_observe(db: Session, attempt: models.ReviewSession,
                       version: models.TrajectoryVersion,
                       made: list[models.TrajectoryStep], world) -> None:
    """Give the new steps a checkpoint chain, and record WHAT THEY CHANGED.

    `before` of each step is the `after` of the one before it, rooted at the
    attempt's seeded initial checkpoint. Without that chain a later fork on a
    human step has no state to restore to, and the replay gate silently degrades
    into "replay on top of whatever is in the browser right now".

    Only ONE world is read per batch — reading per step would put a gym
    round-trip between the annotator and every click. So the observation belongs
    to the batch, and `delta_span` says so: every step names the window, and only
    the step the world was read after carries the delta. A step with
    `world_delta = None` was not observed; that is different from "changed
    nothing", and conflating the two would teach a model that half its actions
    were no-ops.

    Two things the single read can still say honestly, and used to not:

    * when the window changed NOTHING, that holds for every step in it, not just
      the last — so a batch of typing no longer ships three fills with no
      observation at all;
    * when it did change, the delta is attributed to the last step that could
      possibly have caused it. A tab switch cannot place an order, and a batch
      ending in one used to hang the order on the switch.

    The delta is computed against the world the batch STARTED from (the cursor
    checkpoint), so the first batch of an attempt diffs against the seeded world
    rather than reporting the entire world as newly added.

    Hash-first: when the world did not move we record the unchanged delta and
    capture NO checkpoint. That is a net reduction in write volume — every
    exploration batch used to write a full world blob.
    """
    if not made:
        return
    prev = db.scalar(
        select(models.TrajectoryStep)
        .where(models.TrajectoryStep.version_id == version.id,
               models.TrajectoryStep.after_checkpoint_id.is_not(None),
               models.TrajectoryStep.id.not_in([s.id for s in made]))
        .order_by(models.TrajectoryStep.suffix_ordinal.desc())
    )
    cursor = prev.after_checkpoint_id if prev is not None else attempt.initial_checkpoint_id
    for st in made:
        st.before_checkpoint_id = cursor

    # Name the observation window on every step in the batch, whether or not the
    # world can be read — the grouping is a fact about how they were folded.
    span = {"stepIds": [str(s.id) for s in made],
            "observed": "step" if len(made) == 1 else "window"}
    for st in made:
        st.delta_span = span

    if world is None:
        return
    w = _read_world(world)
    if not w:
        return

    prev_hash, prev_world = worlddiff.previous_world(db, attempt, cursor)
    if prev_hash and prev_hash == checkpoints.hash_world(w):
        # The world reads exactly as the batch started. Either the actions changed
        # nothing, or the engine has not applied them yet — this runs milliseconds
        # after the ack, in a race with the mock UI's own POST. Ask again before
        # concluding no-change; the alternative is recording "your click did
        # nothing" whenever we lose the race, which is worse than saying nothing.
        late = _confirm_unchanged(world, prev_hash, made)
        if late is UNREADABLE:
            # We asked and got nothing back. Leave every delta NULL — which the
            # UI and the export both already read as NOT OBSERVED — instead of
            # asserting the actions did nothing.
            return
        if late is None:
            # Genuinely unchanged, and that is true of every step in the window:
            # the world is the same before and after all of them.
            unchanged = worlddiff.diff_worlds(w, w)
            for st in made:
                st.world_delta = unchanged
            db.flush()
            return
        w = late

    cp = checkpoints.capture(db, attempt_id=attempt.id, world=w,
                             step_clock=int(w.get("step") or 0) if isinstance(w, dict) else 0)
    owner = _likely_cause(made)
    owner.after_checkpoint_id = cp.id
    owner.world_after = w
    owner.world_delta = worlddiff.diff_worlds(prev_world, w)
    db.flush()


def _read_world(world) -> dict | None:
    try:
        return world.world()
    except Exception:  # noqa: BLE001 — a world we cannot read must not lose the steps
        return None


#: The confirm read did not come back, so nothing is known. Deliberately NOT
#: None — see `_confirm_unchanged`.
UNREADABLE = object()


def _confirm_unchanged(world, prev_hash: str, made: list[models.TrajectoryStep]):
    """Re-read the world once. THREE outcomes, and they have to stay three.

    * a world dict — it moved after all; we had lost the race
    * ``None``      — asked twice, still unchanged. The ONLY answer that earns a
                      ``{"changed": false}`` on a step.
    * ``UNREADABLE`` — the read did not come back, so we know nothing.

    Collapsing the last two is the very bug this function was added to remove,
    wearing a new hat. `_read_world` returns None on any failure, and
    `gym_client._req` returns None rather than raising on a timeout or a bad
    payload — so one flaky read on the confirm hop asserted "your actions changed
    nothing" across the whole window. The FIRST read already gets this right and
    says nothing; this one has to agree with it.

    A batch that cannot have reached the engine at all skips the wait: there is
    no race to lose.
    """
    if all(st.action_type in _BROWSER_ONLY for st in made):
        return None
    if CONFIRM_MS:
        time.sleep(CONFIRM_MS / 1000)
    late = _read_world(world)
    if not late:
        return UNREADABLE
    return None if checkpoints.hash_world(late) == prev_hash else late


def _likely_cause(made: list[models.TrajectoryStep]) -> models.TrajectoryStep:
    """The step in the window a change can honestly be hung on: the last one that
    reaches the gym engine at all. Falls back to the last step, which is all there
    is to say when the whole window was viewport motion (an async scheduled event
    can still land during one)."""
    for st in reversed(made):
        if st.action_type not in _BROWSER_ONLY:
            return st
    return made[-1]


def materialize(db: Session, attempt: models.ReviewSession, *, now_ms: int | None = None,
                world=None) -> list[models.TrajectoryStep]:
    """Fold this attempt's new raw events into steps on its head version.

    Idempotent and safe to call after every batch. Returns the steps created.
    `world` is the attempt's WorldPort; when given, the batch is checkpointed so
    the steps carry the world their actions produced.
    """
    version = versions.head(db, attempt)
    if version is None:
        # An attempt with recorded interactions HAS a trajectory, by definition.
        # Depending on `/live` having run first would silently discard everything
        # an annotator did whenever the browser opened late or the gym pool was
        # full — the events are already durable, so the version must exist too.
        # (No checkpoint here: only the opener knows the seeded world.)
        version = versions.ensure_manual_root(db, attempt)
    watermark = int(version.materialized_through_seq or 0)
    rows = _events(db, attempt.id, watermark)
    if not rows:
        return []

    settled = _settled(rows, now_ms if now_ms is not None else _now_ms())
    if not settled:
        return []

    actions = recorder.coalesce(settled)
    # Everything folded is consumed, whether or not it produced a step — dropped
    # jitter must still advance the watermark or it is re-examined forever.
    consumed = max(int(e.seq) for e in settled)

    traj = _attempt_trajectory(db, attempt)
    by_seq = {int(e.seq): e for e in settled}
    made: list[models.TrajectoryStep] = []

    for a in actions:
        kind = a.get("kind", "")
        if kind in _NON_STEP or kind == "press_incomplete":
            continue
        sources = [s for s in (a.get("sources") or []) if s is not None]
        first = by_seq.get(min(sources)) if sources else None
        payload = a.get("payload") or {}
        # The pixels the annotator saw when they acted. Frames arrive on their own
        # endpoint and may land before OR after the step exists, so both sides
        # look for the other: here, and in POST /frames.
        shot = ""
        shot_id = None
        for sq in sorted(sources):
            ev = by_seq.get(sq)
            aid = (ev.payload or {}).get("screenshotArtifactId") if ev is not None else None
            if aid:
                shot_id = aid
                art = db.get(models.Artifact, UUID(str(aid))) if aid else None
                shot = blobstore.api_url(art.id) if art is not None else ""
                break
        # `needs_value` is a step we deliberately refuse to certify: its value was
        # redacted at record time, so replaying it would type a placeholder.
        # A kind outside the executor's vocabulary (a drag, a right-click) is the
        # other thing that cannot be proven, and saying so HERE rather than at
        # certify is the difference between the annotator learning it now and
        # learning it at the end of the task.
        state, error = "unverified", ""
        if a.get("needsValue"):
            state = "needs_value"
        elif kind in recorder.OBSERVATION_KINDS:
            # A step that records what the annotator READ. There is nothing to
            # replay and therefore nothing that failed — condemning it here would
            # make one text selection cost a whole task's work. It stays
            # `unverified`, which is exactly what it is.
            pass
        elif kind not in recorder.EXECUTOR_KINDS:
            state, error = "failed", f"the executor has no {kind!r} action, so this step cannot be replayed"
        st = versions.append_step(
            db, version, trajectory_id=traj.id, actor="human",
            action_type=kind,
            description=_describe(a),
            semantic_locator=recorder.semantic_locator(a.get("target") or {}),
            arguments={k: v for k, v in payload.items() if k != "t"},
            url_after=a.get("url") or "",
            tab_id=(a.get("tab") or "")[:32],
            # nx/ny belong in coordinate_fallback, not smuggled into arguments:
            # a locator is what replays, a coordinate is only the last resort.
            coordinate_fallback={k: payload[k] for k in ("nx", "ny") if k in payload},
            intervention_at=_acted_at(first),
            screenshot_url=shot,
            marks_artifact_id=(UUID(str(shot_id)) if shot_id else None),
            replay_state=state,
            replay_error=error,
        )
        # Link every raw event to the step it became, so the two layers stay
        # navigable in both directions and nothing is folded twice.
        for s in sources:
            ev = by_seq.get(s)
            if ev is not None:
                ev.committed_step_id = st.id
        made.append(st)

    _chain_and_observe(db, attempt, version, made, world)

    # SAME transaction as the steps: that is the idempotency.
    version.materialized_through_seq = consumed
    db.flush()
    return made


def _attempt_trajectory(db: Session, attempt: models.ReviewSession) -> models.Trajectory:
    """The attempt's own manual trajectory, created on demand."""
    traj = db.scalar(
        select(models.Trajectory)
        .where(models.Trajectory.session_id == attempt.id,
               models.Trajectory.source == "manual")
        .order_by(models.Trajectory.created_at)
    )
    if traj is None:
        traj = models.Trajectory(session_id=attempt.id, agent="human",
                                 seed=attempt.seed, source="manual")
        db.add(traj)
        db.flush()
    return traj
