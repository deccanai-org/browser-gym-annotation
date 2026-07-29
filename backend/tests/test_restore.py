"""Prefix restore — bringing a fork's world up to the fork point on open.

The contract: on a FORK, replay the branch prefix into the freshly seeded world;
on anything else, do nothing. Best-effort, so a prefix that cannot fully replay
gets the annotator as far as it can and reports how far — the strict gates are
finalize and commit, not this.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import models, restore, versions
from app.config import settings


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"RS-{uuid4().hex[:8]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"rs-{uuid4().hex[:8]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.flush()
    tr = models.Trajectory(session_id=s.id, agent="gpt-5.5", source="gym")
    db_session.add(tr)
    db_session.commit()
    s.trajectory = tr
    return s


def _root(db, attempt, n=4):
    v1 = versions.create_root(db, attempt_id=attempt.id, base_trajectory_id=attempt.trajectory.id, producer="gpt-5.5")
    steps = []
    for i in range(n):
        st = models.TrajectoryStep(
            trajectory_id=attempt.trajectory.id, idx=i, action_type="click",
            description=f"step {i}", actor="agent",
            semantic_locator={"testId": f"btn-{i}"},
        )
        db.add(st)
        steps.append(st)
    db.flush()
    versions.adopt_steps(db, v1, steps, actor="agent")
    db.commit()
    return v1, steps


def _make_head(db, attempt, version):
    attempt.active_version_id = version.id
    db.commit()


class FakeExecutor:
    """Records the actions it was asked to replay; every action lands."""

    def __init__(self, fail_at=None):
        self.acted = []
        self.fail_at = fail_at

    def act(self, kind, locator, args):
        i = len(self.acted)
        self.acted.append((kind, locator, args))
        if self.fail_at is not None and i == self.fail_at:
            return {"ok": False, "error": "did not land"}
        return {"ok": True, "resolved": {"url": "/x"}}

    def world(self):
        return {"step": len(self.acted)}


class FakeGym:
    def world(self):
        return {}                       # no scheduled events
    def verify(self, i):
        pass


# --------------------------------------------------------------------------- what has a prefix
def test_an_unforked_attempt_has_no_prefix(db_session, attempt):
    v1, _ = _root(db_session, attempt)
    _make_head(db_session, attempt, v1)
    actions, why = restore.prefix_of(db_session, attempt)
    assert actions == []
    assert why == "not a fork"


def test_a_fork_prefix_is_the_steps_before_the_fork(db_session, attempt):
    v1, steps = _root(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[2])
    _make_head(db_session, attempt, v2)
    actions, why = restore.prefix_of(db_session, attempt)
    assert why == ""
    assert [a["kind"] for a in actions] == ["click", "click"], "the two steps before the fork"


def test_restoring_an_unforked_attempt_does_nothing(db_session, attempt):
    v1, _ = _root(db_session, attempt)
    _make_head(db_session, attempt, v1)
    ex = FakeExecutor()
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert not report.attempted
    assert ex.acted == [], "an unforked attempt must not replay the canonical run into the world"


# --------------------------------------------------------------------------- the happy path
def test_a_fork_prefix_is_replayed_in_order(db_session, attempt):
    v1, steps = _root(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[3])  # 3 steps of prefix
    _make_head(db_session, attempt, v2)
    ex = FakeExecutor()
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert report.attempted and not report.partial
    assert report.done == report.total == 3
    assert [locator["testId"] for _, locator, _ in ex.acted] == ["btn-0", "btn-1", "btn-2"]


# --------------------------------------------------------------------------- partial, not fatal
def test_a_step_that_does_not_land_stops_the_rebuild_without_raising(db_session, attempt):
    v1, steps = _root(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[3])
    _make_head(db_session, attempt, v2)
    ex = FakeExecutor(fail_at=1)  # second action fails
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert report.attempted and report.partial
    assert report.done == 1 and report.total == 3
    assert report.reason, "a partial rebuild must say why it stopped"


def test_a_locator_free_step_does_not_truncate_the_rebuild(db_session, attempt):
    """A `wait`/`scroll` carries no locator but the live service runs it anyway.
    finalize.replayable() would truncate here; the rebuild must not."""
    v1 = versions.create_root(db_session, attempt_id=attempt.id, base_trajectory_id=attempt.trajectory.id, producer="gpt-5.5")
    kinds = ["click", "wait", "scroll", "click"]
    steps = []
    for i, k in enumerate(kinds):
        st = models.TrajectoryStep(
            trajectory_id=attempt.trajectory.id, idx=i, action_type=k,
            description=k, actor="agent",
            semantic_locator={"testId": f"btn-{i}"} if k == "click" else {},
        )
        db_session.add(st)
        steps.append(st)
    db_session.flush()
    versions.adopt_steps(db_session, v1, steps, actor="agent")
    db_session.commit()
    v2 = versions.fork_before(db_session, parent=v1, step=steps[3])  # prefix: click, wait, scroll
    _make_head(db_session, attempt, v2)
    ex = FakeExecutor()
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert report.done == 3, "wait/scroll carry no locator but still replay"
    assert not report.partial


def test_the_cap_bounds_the_rebuild(db_session, attempt, monkeypatch):
    monkeypatch.setattr(settings, "live_restore_max_steps", 2)
    v1, steps = _root(db_session, attempt, n=6)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[5])  # 5-step prefix
    _make_head(db_session, attempt, v2)
    ex = FakeExecutor()
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert len(ex.acted) == 2, "must not replay past the cap"
    assert report.partial and "limit" in report.reason


def test_the_kill_switch_disables_the_rebuild(db_session, attempt, monkeypatch):
    monkeypatch.setattr(settings, "live_restore_max_steps", 0)
    v1, steps = _root(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[2])
    _make_head(db_session, attempt, v2)
    ex = FakeExecutor()
    report = restore.restore_prefix(db_session, attempt, executor=ex, gym=FakeGym())
    assert not report.attempted and ex.acted == []


# --------------------------------------------------------------------------- payload shape
def test_no_attempt_renders_as_nothing_not_zero_of_zero():
    assert restore.as_payload(restore.RestoreReport()) is None


def test_a_completed_rebuild_reports_done_equals_total():
    payload = restore.as_payload(restore.RestoreReport(done=3, total=3, attempted=True))
    assert payload == {"done": 3, "total": 3, "partial": False, "reason": ""}


def test_a_partial_rebuild_is_flagged():
    payload = restore.as_payload(restore.RestoreReport(done=1, total=3, reason="stopped", attempted=True))
    assert payload["partial"] is True and payload["reason"] == "stopped"
