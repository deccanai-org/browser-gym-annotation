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

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import checkpoints, models, recorder, versions

# An event newer than this may still be part of an edit in progress. Comfortably
# above the 1.5s keystroke-coalescing window, so a slow typist's word is not split
# across two steps.
SETTLE_MS = 2000

# Kinds that never become a step on their own: they are folded into one, or they
# are exploration that carries no world change.
_NON_STEP = {"mouseDown", "mouseUp", "mousePressed", "mouseReleased", "keyChar", "move"}


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _events(db: Session, attempt_id: UUID, after_seq: int) -> list[models.InteractionEvent]:
    return list(db.scalars(
        select(models.InteractionEvent)
        .where(models.InteractionEvent.attempt_id == attempt_id,
               models.InteractionEvent.seq > after_seq)
        .order_by(models.InteractionEvent.seq)
    ))


def _settled(rows: list[models.InteractionEvent], now_ms: int) -> list[models.InteractionEvent]:
    """The prefix that is safe to fold — see "withhold the open tail" above."""
    cut = len(rows)
    while cut > 0:
        t = int((rows[cut - 1].payload or {}).get("t") or 0)
        if t and now_ms - t < SETTLE_MS:
            cut -= 1
            continue
        break
    return rows[:cut]


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
    if kind == "scroll":
        dy = payload.get("dy", 0)
        return f"scroll {'down' if (dy or 0) > 0 else 'up'}" + (f" in {name}" if name else "")
    if kind in ("click", "dblclick") and payload.get("button") == "right":
        return f"right-click {name}".strip()
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


def _chain_checkpoints(db: Session, attempt: models.ReviewSession,
                       version: models.TrajectoryVersion,
                       made: list[models.TrajectoryStep], world) -> None:
    """Give the new steps a checkpoint chain, and the last one the world.

    `before` of each step is the `after` of the one before it, rooted at the
    attempt's seeded initial checkpoint. Without that chain a later fork on a
    human step has no state to restore to, and the replay gate silently degrades
    into "replay on top of whatever is in the browser right now".

    Only ONE world is read per batch, and it is attached to the LAST step —
    reading per step would put a gym round-trip between the annotator and every
    click. Intermediate steps therefore carry no `after` checkpoint, which is the
    honest thing to record: we did not observe one. Nothing fabricates a hash.
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

    if world is None:
        return
    try:
        w = world.world()
    except Exception:  # noqa: BLE001 — a world we cannot read must not lose the steps
        w = None
    if not w:
        return
    cp = checkpoints.capture(db, attempt_id=attempt.id, world=w,
                             step_clock=int(w.get("step") or 0) if isinstance(w, dict) else 0)
    made[-1].after_checkpoint_id = cp.id
    made[-1].world_after = w
    db.flush()


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
                shot = art.uri if art is not None else ""
                break
        # `needs_value` is a step we deliberately refuse to certify: its value was
        # redacted at record time, so replaying it would type a placeholder.
        state = "needs_value" if a.get("needsValue") else "unverified"
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
            intervention_at=(first.occurred_at if first is not None else None),
            screenshot_url=shot,
            marks_artifact_id=(UUID(str(shot_id)) if shot_id else None),
            replay_state=state,
        )
        # Link every raw event to the step it became, so the two layers stay
        # navigable in both directions and nothing is folded twice.
        for s in sources:
            ev = by_seq.get(s)
            if ev is not None:
                ev.committed_step_id = st.id
        made.append(st)

    _chain_checkpoints(db, attempt, version, made, world)

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
