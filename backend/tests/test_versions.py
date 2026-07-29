"""The version graph: suffix inheritance, fork-before, and optimistic heads.

The load-bearing claim under test is that inherited steps are REFERENCED, not
copied — everything else (verdicts surviving a re-fork, stable display numbers,
correct lineage) follows from it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import models, versions


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"VG-{uuid4().hex[:8]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"vg-{uuid4().hex[:8]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.flush()
    t = models.Trajectory(session_id=s.id, agent="gpt-5.5", source="gym")
    db_session.add(t)
    db_session.commit()
    s.trajectory = t  # convenience handle for the tests
    return s


def _root_with_steps(db, attempt, n=4):
    v1 = versions.create_root(db, attempt_id=attempt.id, base_trajectory_id=attempt.trajectory.id, producer="gpt-5.5")
    steps = []
    for i in range(n):
        st = models.TrajectoryStep(
            trajectory_id=attempt.trajectory.id, idx=i, action_type="click",
            description=f"agent step {i}", actor="agent",
        )
        db.add(st)
        steps.append(st)
    db.flush()
    versions.adopt_steps(db, v1, steps, actor="agent")
    db.commit()
    return v1, steps


def _human_step(db, attempt, version, desc, **kw):
    return versions.append_step(
        db, version, trajectory_id=attempt.trajectory.id, actor="human",
        action_type="click", description=desc, **kw,
    )


# --------------------------------------------------------------------------- root
def test_the_root_binds_the_exact_base_run(db_session, attempt):
    v1, steps = _root_with_steps(db_session, attempt)
    assert v1.version_no == 1 and v1.parent_version_id is None
    assert v1.base_trajectory_id == attempt.trajectory.id, "canonical must be BOUND, not guessed"
    assert [s.suffix_ordinal for s in steps] == [0, 1, 2, 3]
    assert len(versions.flatten(db_session, v1)) == 4


# --------------------------------------------------------------------------- fork semantics
def test_fork_before_drops_the_rejected_step(db_session, attempt):
    """The correction's entire purpose: the bad action must be gone from the child."""
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[2])
    _human_step(db_session, attempt, v2, "corrected action")
    db_session.commit()

    flat = versions.flatten(db_session, v2)
    ids = [s.id for s in flat]
    assert steps[2].id not in ids, "the rejected step leaked into the child"
    assert steps[3].id not in ids, "everything after the fork point is replaced too"
    assert ids[:2] == [steps[0].id, steps[1].id]
    assert flat[-1].description == "corrected action" and flat[-1].actor == "human"


def test_continue_after_keeps_the_step_and_starts_from_its_after_state(db_session, attempt):
    """fork-before and continue-after must yield different, correct parents."""
    v1, steps = _root_with_steps(db_session, attempt)
    before = models.EnvironmentCheckpoint(attempt_id=attempt.id, world={"a": 1})
    after = models.EnvironmentCheckpoint(attempt_id=attempt.id, world={"a": 2})
    db_session.add_all([before, after])
    db_session.flush()
    steps[2].before_checkpoint_id, steps[2].after_checkpoint_id = before.id, after.id
    db_session.flush()

    rejected = versions.fork_before(db_session, parent=v1, step=steps[2])
    kept = versions.continue_after(db_session, parent=v1, step=steps[2])
    db_session.commit()

    assert steps[2].id not in [s.id for s in versions.flatten(db_session, rejected)]
    assert [s.id for s in versions.flatten(db_session, kept)] == [s.id for s in steps[:3]]
    assert rejected.fork_checkpoint_id == before.id
    assert kept.fork_checkpoint_id == after.id, "continuing must resume from the AFTER state"


def test_continue_after_the_last_step_inherits_the_whole_parent(db_session, attempt):
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.continue_after(db_session, parent=v1, step=steps[-1])
    _human_step(db_session, attempt, v2, "finish the task")
    db_session.commit()
    assert [s.id for s in versions.flatten(db_session, v2)][:4] == [s.id for s in steps]
    assert len(versions.flatten(db_session, v2)) == 5


def test_forking_on_a_foreign_step_is_refused(db_session, attempt):
    """Failing closed beats minting a version whose prefix cannot be reconstructed."""
    v1, _ = _root_with_steps(db_session, attempt)
    stray = models.TrajectoryStep(trajectory_id=attempt.trajectory.id, idx=99, description="not mine")
    db_session.add(stray)
    db_session.flush()
    with pytest.raises(versions.LineageError):
        versions.fork_before(db_session, parent=v1, step=stray)


# --------------------------------------------------------------------------- inheritance
def test_inherited_steps_are_the_same_rows_not_copies(db_session, attempt):
    """If a child copied its prefix, every inherited step would get a new UUID and
    silently lose its verdict, its screenshot binding and its checkpoints."""
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[3])
    _human_step(db_session, attempt, v2, "replacement")
    db_session.commit()

    inherited = versions.flatten(db_session, v2)[:3]
    assert [s.id for s in inherited] == [s.id for s in steps[:3]]
    assert all(s.version_id == v1.id for s in inherited), "inherited steps still belong to v1"
    assert db_session.query(models.TrajectoryStep).count() == 5, "3 inherited + 1 dropped + 1 new"


def test_per_step_verdicts_survive_a_re_fork(db_session, attempt):
    """THE reason identity is a UUID. The annotator verified steps 0-1, then forked
    twice; their work must still be attached afterwards."""
    v1, steps = _root_with_steps(db_session, attempt)
    versions.set_verdict(db_session, attempt_id=attempt.id, step_id=steps[0].id, verdict="verified")
    versions.set_verdict(db_session, attempt_id=attempt.id, step_id=steps[1].id, verdict="verified")
    versions.set_verdict(db_session, attempt_id=attempt.id, step_id=steps[2].id, verdict="rejected", note="wrong item")

    v2 = versions.fork_before(db_session, parent=v1, step=steps[2])
    fix = _human_step(db_session, attempt, v2, "add the right item")
    versions.set_verdict(db_session, attempt_id=attempt.id, step_id=fix.id, verdict="verified")
    v3 = versions.fork_before(db_session, parent=v2, step=steps[1])  # re-fork EARLIER
    _human_step(db_session, attempt, v3, "different route")
    db_session.commit()

    got = versions.verdicts_for(db_session, attempt.id)
    assert got[str(steps[0].id)]["verdict"] == "verified", "verdicts must not be clamped away by a re-fork"
    assert got[str(steps[2].id)]["note"] == "wrong item"
    # v3 forked before step 1, so step 1 is gone from the branch — but the verdict
    # row still describes the step it was made about.
    assert [s.id for s in versions.flatten(db_session, v3)] == [steps[0].id, versions.flatten(db_session, v3)[-1].id]


def test_display_numbers_are_computed_not_stored(db_session, attempt):
    """The same step sits at different positions on different branches, so position
    cannot be identity."""
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[1])
    a = _human_step(db_session, attempt, v2, "A")
    b = _human_step(db_session, attempt, v2, "B")
    db_session.commit()

    view = versions.flat_view(db_session, v2)
    assert [v["displayIdx"] for v in view] == [0, 1, 2]
    assert [v["inherited"] for v in view] == [True, False, False]
    assert [v["stepId"] for v in view] == [str(steps[0].id), str(a.id), str(b.id)]
    assert a.suffix_ordinal == 0 and b.suffix_ordinal == 1, "ordinals are LOCAL to the version"


def test_a_hybrid_trajectory_reports_per_step_actors(db_session, attempt):
    """Version-level `kind` is not enough — a corrected run is agent prefix +
    human suffix, and the export has to say which is which."""
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[2])
    _human_step(db_session, attempt, v2, "human fix")
    db_session.commit()
    assert [s["actor"] for s in versions.flat_view(db_session, v2)] == ["agent", "agent", "human"]


def test_lineage_is_v1_v2_v3(db_session, attempt):
    v1, steps = _root_with_steps(db_session, attempt)
    v2 = versions.fork_before(db_session, parent=v1, step=steps[3])
    _human_step(db_session, attempt, v2, "x")
    db_session.commit()
    v3 = versions.continue_after(db_session, parent=v2, step=versions.flatten(db_session, v2)[-1])
    _human_step(db_session, attempt, v3, "y")
    db_session.commit()

    assert [v.version_no for v in versions.chain(db_session, v3)] == [1, 2, 3]
    assert len(versions.flatten(db_session, v3)) == 5  # 3 inherited + x + y
    assert [v.version_no for v in versions.versions_for(db_session, attempt.id)] == [1, 2, 3]


# --------------------------------------------------------------------------- concurrency
def test_head_advances_only_under_compare_and_swap(db_session, attempt):
    v1, steps = _root_with_steps(db_session, attempt)
    rev = versions.set_head(db_session, attempt, v1, expected_revision=0)
    assert rev == 1 and attempt.active_version_id == v1.id

    with pytest.raises(versions.ConcurrencyError):
        versions.set_head(db_session, attempt, v1, expected_revision=0)  # stale read


def test_a_late_agent_run_cannot_resurrect_a_superseded_branch(db_session, attempt):
    """Out-of-order completion is the realistic failure: a slow job returns after
    the annotator already moved the head. It must lose."""
    v1, steps = _root_with_steps(db_session, attempt)
    slow = versions.fork_before(db_session, parent=v1, step=steps[1])   # job started here
    seen_revision = attempt.revision

    fast = versions.fork_before(db_session, parent=v1, step=steps[2])
    versions.set_head(db_session, attempt, fast, expected_revision=seen_revision)
    db_session.commit()

    with pytest.raises(versions.ConcurrencyError):
        versions.set_head(db_session, attempt, slow, expected_revision=seen_revision)
    assert attempt.active_version_id == fast.id


def test_status_transitions_are_guarded_and_validated(db_session, attempt):
    v1, _ = _root_with_steps(db_session, attempt)
    assert v1.status == "candidate", "a completed run is a CANDIDATE, never auto-approved"
    versions.set_status(db_session, v1, "approved", expected_revision=0)
    assert v1.status == "approved"

    with pytest.raises(versions.ConcurrencyError):
        versions.set_status(db_session, v1, "rejected", expected_revision=0)
    with pytest.raises(ValueError):
        versions.set_status(db_session, v1, "definitely-fine", expected_revision=1)


def test_selecting_a_version_from_another_attempt_is_refused(db_session, attempt):
    v1, _ = _root_with_steps(db_session, attempt)
    other = models.ReviewSession(task_id=attempt.task_id, annotator_id=attempt.annotator_id, source="gym")
    db_session.add(other)
    db_session.flush()
    with pytest.raises(versions.LineageError):
        versions.set_head(db_session, other, v1, expected_revision=0)


# --------------------------------------------------------------------------- cap
def test_the_rerun_counter_lives_on_the_attempt(db_session, attempt):
    """Counting per branch would let an annotator reset the cap by forking."""
    v1, steps = _root_with_steps(db_session, attempt)
    versions.fork_before(db_session, parent=v1, step=steps[1])
    assert versions.count_agent_call(db_session, attempt) == 1
    assert versions.count_agent_call(db_session, attempt) == 2
    assert versions.count_agent_call(db_session, attempt, counts=False) == 2, (
        "a confirmed infrastructure failure must not burn a run"
    )
