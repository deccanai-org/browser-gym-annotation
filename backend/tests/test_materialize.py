"""Steps appear as the annotator works — and only once.

The contract this locks in: recording an interaction and PROVING it replays are
separate. Folding is continuous, idempotent, and never touches the browser.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app import materialize, models, recorder, versions


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"MT-{uuid4().hex[:8]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"mt-{uuid4().hex[:8]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym", mode="human_do")
    db_session.add(s)
    db_session.commit()
    return s


def _ev(db, attempt, i, kind, t, target, **payload):
    return recorder.record_event(
        db, attempt_id=attempt.id, kind=kind, payload={"t": t, **payload},
        target=target, url="https://shop.gym.local/", tab="t1",
        client_event_id=f"c{i}",
    )


BTN = {"targetKey": "btn-cart", "testId": "btn-cart"}
BOX = {"targetKey": "input-q", "testId": "input-q"}


def _settled_base() -> int:
    """Well in the past, so nothing is withheld as an edit in progress."""
    return int(time.time() * 1000) - 60_000


def test_interactions_become_steps_without_anyone_pressing_commit(db_session, attempt):
    t0 = _settled_base()
    _ev(db_session, attempt, 1, "mouseDown", t0, BTN, nx=0.5, ny=0.5, button="left")
    _ev(db_session, attempt, 2, "mouseUp", t0 + 40, BTN, nx=0.5, ny=0.5, button="left", clicks=1)
    _ev(db_session, attempt, 3, "keyChar", t0 + 500, BOX, text="m", value="m")
    _ev(db_session, attempt, 4, "keyPress", t0 + 600, BOX, key="Backspace", value="")
    _ev(db_session, attempt, 5, "keyChar", t0 + 700, BOX, text="s", value="s")
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()

    assert [s.action_type for s in made] == ["click", "fill"]
    # The value is the PAGE's, so the Backspace is already applied.
    assert made[1].arguments["value"] == "s"
    # Every raw event points at the step it became, so the two layers stay
    # navigable in both directions.
    evs = db_session.query(models.InteractionEvent).filter_by(attempt_id=attempt.id).all()
    assert all(e.committed_step_id is not None for e in evs)


def test_folding_twice_does_not_duplicate_the_trajectory(db_session, attempt):
    """There is no Commit button to double-click, because there is no button."""
    t0 = _settled_base()
    _ev(db_session, attempt, 1, "mouseDown", t0, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", t0 + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()

    first = materialize.materialize(db_session, attempt)
    db_session.commit()
    again = materialize.materialize(db_session, attempt)
    db_session.commit()

    assert len(first) == 1 and again == []
    total = db_session.query(models.TrajectoryStep).count()
    assert total == 1, "re-folding appended a second copy"


def test_an_edit_still_in_progress_is_not_committed_yet(db_session, attempt):
    """A fill mid-word must not become a step, and then a SECOND step for the
    rest of the word once the annotator finishes typing."""
    now = int(time.time() * 1000)
    _ev(db_session, attempt, 1, "keyChar", now - 50, BOX, text="m", value="m")
    db_session.commit()

    assert materialize.materialize(db_session, attempt, now_ms=now) == []

    # Once it has settled, it folds — as ONE step.
    made = materialize.materialize(db_session, attempt, now_ms=now + materialize.SETTLE_MS + 1)
    db_session.commit()
    assert [s.action_type for s in made] == ["fill"]


def test_a_redacted_value_is_marked_rather_than_replayed(db_session, attempt):
    """It must not certify: replaying it would type the placeholder into the field."""
    t0 = _settled_base()
    pw = {"targetKey": "pw", "testId": "input-password", "type": "password"}
    _ev(db_session, attempt, 1, "keyChar", t0, pw, text="s", value="hunter2")
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert made[0].replay_state == "needs_value"
    assert made[0].arguments.get("value") is None


def test_only_a_human_do_attempt_folds_its_own_interactions(db_session, attempt):
    """In the AGENT-review flow the trajectory is the agent's run, and the
    annotator's clicking is exploration — folding it in would put their poking
    about into the golden. The separation is the whole point of the two tables."""
    attempt.mode = "agent_review"
    db_session.commit()
    assert materialize.should_materialize(attempt) is False
    attempt.mode = "human_do"
    assert materialize.should_materialize(attempt) is True


def test_events_recorded_before_a_browser_opened_are_not_lost(db_session, attempt):
    """The gym pool can be full when an annotator starts. Their interactions are
    already durable, so the trajectory must materialise anyway rather than
    silently discarding the work."""
    assert attempt.active_version_id is None
    t0 = _settled_base()
    _ev(db_session, attempt, 1, "navigate", t0, {}, url="https://shop.gym.local/cart")
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert [s.action_type for s in made] == ["navigate"]
    assert versions.head(db_session, attempt) is not None


# --------------------------------------------------------------------------- certify
# Recording a step and PROVING it are separate claims. These lock in the part that
# matters: proving must never cost the annotator their work.

def test_a_step_whose_value_was_redacted_will_not_certify(db_session, attempt):
    """Replaying it would type the placeholder into the field — and the old gate
    passed exactly that, shipping a green but broken golden."""
    t0 = _settled_base()
    pw = {"targetKey": "pw", "testId": "input-password", "type": "password"}
    _ev(db_session, attempt, 1, "keyChar", t0, pw, text="s", value="hunter2")
    db_session.commit()
    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert made[0].replay_state == "needs_value"


def test_certification_marks_steps_rather_than_deleting_them(db_session, attempt):
    """A step that does not replay is evidence, not rubbish. The old commit path
    threw the WHOLE sequence away when one action failed."""
    t0 = _settled_base()
    _ev(db_session, attempt, 1, "mouseDown", t0, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", t0 + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()
    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert made[0].replay_state == "unverified"

    # What certification does to a failing step, expressed directly.
    made[0].replay_state = "diverged"
    made[0].replay_error = "no element matched [data-test-id=btn-cart]"
    db_session.commit()

    still = db_session.query(models.TrajectoryStep).filter_by(id=made[0].id).one()
    assert still.replay_state == "diverged"
    assert still.replay_error
    assert db_session.query(models.TrajectoryStep).count() == 1, "a diverged step was deleted"


# --------------------------------------------------------------------- state deltas

class _FakeWorld:
    """A WorldPort that hands back a scripted sequence of worlds — one per read."""

    def __init__(self, *worlds):
        self._worlds = list(worlds)
        self.reads = 0

    def world(self):
        w = self._worlds[min(self.reads, len(self._worlds) - 1)]
        self.reads += 1
        return w


def _w(orders=None, cart=None, unread=0):
    return {
        "task_id": "MT", "seed": 0, "step": 1, "finished": False,
        "shop": {"cart": {"items": cart or [], "applied_promo": None},
                 "orders": orders or {}},
        "mail": {"inbox": {}, "sent": {}, "unread_count": unread},
        "events": [],
    }


def _seed_initial(db, attempt, world):
    from app import checkpoints
    cp = checkpoints.capture(db, attempt_id=attempt.id, world=world, step_clock=0)
    attempt.initial_checkpoint_id = cp.id
    db.commit()
    return cp


def test_a_step_records_the_state_change_its_action_produced(db_session, attempt):
    """The whole point: the trajectory says what the world DID, not where the
    mouse was."""
    base = _settled_base()
    empty = _w()
    ordered = _w(orders={"ORD_7": {"id": "ORD_7", "status": "placed", "total": 49.99}})
    _seed_initial(db_session, attempt, empty)

    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()

    made = materialize.materialize(db_session, attempt, world=_FakeWorld(ordered))
    assert len(made) == 1
    d = made[0].world_delta
    assert d and d["changed"] is True
    assert d["apps"] == ["shop"]
    add = next(c for c in d["changes"] if c["op"] == "add")
    assert add["id"] == "ORD_7" and add["path"] == "shop.orders"
    # and it is attributed to exactly this step
    assert made[0].delta_span["observed"] == "step"
    assert made[0].delta_span["stepIds"] == [str(made[0].id)]


def test_a_batch_that_changed_nothing_writes_no_new_checkpoint(db_session, attempt):
    """Exploration must not cost a full world blob per batch — and the annotator
    should still be told their click did nothing."""
    from sqlalchemy import func, select

    base = _settled_base()
    world = _w()
    _seed_initial(db_session, attempt, world)
    before_n = db_session.scalar(
        select(func.count()).select_from(models.EnvironmentCheckpoint)
        .where(models.EnvironmentCheckpoint.attempt_id == attempt.id))

    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()
    made = materialize.materialize(db_session, attempt, world=_FakeWorld(world))

    after_n = db_session.scalar(
        select(func.count()).select_from(models.EnvironmentCheckpoint)
        .where(models.EnvironmentCheckpoint.attempt_id == attempt.id))
    assert after_n == before_n, "an unchanged world must not be checkpointed again"
    assert made[-1].world_delta["changed"] is False
    assert made[-1].after_checkpoint_id is None


def test_a_delta_names_every_step_it_covers(db_session, attempt):
    """Two actions folded in one batch share ONE observation. Attributing the
    change to whichever was last would be a lie, so the span names both and only
    the observed step carries the delta."""
    base = _settled_base()
    _seed_initial(db_session, attempt, _w())

    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 20, BTN, nx=0.5, ny=0.5, clicks=1)
    _ev(db_session, attempt, 3, "mouseDown", base + 40, BOX, nx=0.3, ny=0.3)
    _ev(db_session, attempt, 4, "mouseUp", base + 60, BOX, nx=0.3, ny=0.3, clicks=1)
    db_session.commit()

    made = materialize.materialize(
        db_session, attempt,
        world=_FakeWorld(_w(orders={"O1": {"id": "O1", "status": "placed"}})))
    assert len(made) == 2
    ids = [str(s.id) for s in made]
    for s in made:
        assert s.delta_span["stepIds"] == ids
        assert s.delta_span["observed"] == "window"
    assert made[0].world_delta is None, "an unobserved step must not claim a delta"
    assert made[-1].world_delta["changed"] is True


def test_the_first_batch_diffs_against_the_seeded_world(db_session, attempt):
    """Not against nothing — otherwise step 1 reports the entire world as added."""
    base = _settled_base()
    seeded = _w(orders={"OLD": {"id": "OLD", "status": "delivered"}})
    _seed_initial(db_session, attempt, seeded)

    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()
    after = _w(orders={"OLD": {"id": "OLD", "status": "delivered"},
                       "NEW": {"id": "NEW", "status": "placed"}})
    made = materialize.materialize(db_session, attempt, world=_FakeWorld(after))

    d = made[-1].world_delta
    adds = [c for c in d["changes"] if c["op"] == "add"]
    assert [c["id"] for c in adds] == ["NEW"], "the pre-existing order is not a change"


def test_a_settled_click_folds_without_waiting_out_the_edit_window(db_session, attempt):
    """A click is complete the moment it acks. Withholding it for SETTLE_MS was
    what pushed several actions into one observation window."""
    now = int(time.time() * 1000)
    _ev(db_session, attempt, 1, "mouseDown", now - 50, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", now - 20, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()
    made = materialize.materialize(db_session, attempt, now_ms=now)
    assert [s.action_type for s in made] == ["click"]


def test_a_read_that_lost_the_race_is_not_written_down_as_no_change(db_session, attempt):
    """The world is read milliseconds after the click acks, racing the mock UI's
    own POST to the engine. Losing that race looks exactly like a no-op, and it
    used to be recorded as {"changed": false} — "your click did nothing" — which
    is a lie an SFT target learns from. The confirm re-read is what tells the two
    apart."""
    base = _settled_base()
    before = _w()
    after = _w(orders={"ORD_9": {"id": "ORD_9", "status": "placed"}})
    _seed_initial(db_session, attempt, before)

    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    db_session.commit()

    # The engine applies the click between the two reads.
    world = _FakeWorld(before, after)
    made = materialize.materialize(db_session, attempt, world=world)
    assert world.reads == 2, "an apparent no-op must be confirmed, not believed"
    assert made[-1].world_delta["changed"] is True
    assert made[-1].after_checkpoint_id is not None


def test_an_unchanged_window_is_recorded_on_every_step_in_it(db_session, attempt):
    """Only the tail used to carry the delta, so a batch of typing shipped fills
    with world_delta NULL — "not observed" — when the truth is known: the world is
    the same before and after all of them."""
    monkey = materialize.CONFIRM_MS
    materialize.CONFIRM_MS = 0
    try:
        base = _settled_base()
        _seed_initial(db_session, attempt, _w())
        _ev(db_session, attempt, 1, "keyChar", base, BOX, text="m", value="m")
        _ev(db_session, attempt, 2, "keyChar", base + 80, BOX, text="g", value="mg")
        _ev(db_session, attempt, 3, "keyPress", base + 200, BOX, key="Enter", value="mg")
        db_session.commit()

        made = materialize.materialize(db_session, attempt, world=_FakeWorld(_w()))
        assert [s.action_type for s in made] == ["fill", "press"]
        assert all(s.world_delta is not None and s.world_delta["changed"] is False for s in made)
    finally:
        materialize.CONFIRM_MS = monkey


def test_a_change_is_not_hung_on_a_tab_switch_that_ended_the_batch(db_session, attempt):
    """A tab switch cannot place an order. The delta used to go to whichever step
    happened to be last, which named the wrong action as the cause."""
    base = _settled_base()
    _seed_initial(db_session, attempt, _w())
    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    _ev(db_session, attempt, 3, "switch_tab", base + 60, {"app": "mail"}, app="mail",
        url="https://mail.gym.local/")
    db_session.commit()

    made = materialize.materialize(
        db_session, attempt,
        world=_FakeWorld(_w(orders={"O1": {"id": "O1", "status": "placed"}})))
    assert [s.action_type for s in made] == ["click", "switch_tab"]
    assert made[0].world_delta and made[0].world_delta["changed"] is True
    assert made[1].world_delta is None, "a tab switch did not place the order"


def test_a_gesture_the_executor_cannot_perform_is_marked_when_it_is_folded(db_session, attempt):
    """A right-click has no executor action at all, so it can never be proven.
    Saying so at fold time is the difference between the annotator learning it now
    and learning it after a whole task's work."""
    base = _settled_base()
    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5, button="right")
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, button="right", clicks=1)
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert made[0].action_type == "right_click"
    assert made[0].replay_state == "failed" and "right_click" in made[0].replay_error
    assert made[0].description == "right-click btn-cart"


def test_enter_folds_into_a_step_the_executor_can_actually_run(db_session, attempt):
    base = _settled_base()
    _ev(db_session, attempt, 1, "keyChar", base, BOX, text="m", value="m")
    _ev(db_session, attempt, 2, "keyPress", base + 100, BOX, key="Enter", value="m")
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert [s.action_type for s in made] == ["fill", "press"]
    assert made[1].replay_state == "unverified", "a replayable step must not be pre-failed"
    assert made[1].description == "press Enter in input-q"
    assert made[1].arguments["key"] == "Enter"


def test_a_step_carries_the_time_the_annotator_acted(db_session, attempt):
    """`occurred_at` is when the BATCH was written — the recorder flushes on a
    boundary or a 1.2s timer, so a minute of work came back as a handful of
    identical timestamps and no step had a duration."""
    base = _settled_base()
    _ev(db_session, attempt, 1, "mouseDown", base, BTN, nx=0.5, ny=0.5)
    _ev(db_session, attempt, 2, "mouseUp", base + 30, BTN, nx=0.5, ny=0.5, clicks=1)
    _ev(db_session, attempt, 3, "mouseDown", base + 9_000, BOX, nx=0.3, ny=0.3)
    _ev(db_session, attempt, 4, "mouseUp", base + 9_030, BOX, nx=0.3, ny=0.3, clicks=1)
    db_session.commit()

    made = materialize.materialize(db_session, attempt)
    db_session.commit()
    assert all(s.intervention_at is not None for s in made)
    apart = (made[1].intervention_at - made[0].intervention_at).total_seconds()
    assert 8.9 < apart < 9.1, "the nine seconds the annotator spent reading are gone"


def test_a_trailing_unpaired_press_is_still_withheld(db_session, attempt):
    """The guard on the above: folding a lone mouseDown makes it
    `press_incomplete` and orphans its mouseUp in the next batch."""
    now = int(time.time() * 1000)
    _ev(db_session, attempt, 1, "mouseDown", now - 50, BTN, nx=0.5, ny=0.5)
    db_session.commit()
    assert materialize.materialize(db_session, attempt, now_ms=now) == []
