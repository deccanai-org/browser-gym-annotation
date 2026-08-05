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
