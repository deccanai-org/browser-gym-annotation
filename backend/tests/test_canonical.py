"""Canonical-run binding (§3.10).

Which recorded run a gym task is annotated against was decided by a heuristic —
"the oldest gym trajectory carrying a replay payload" — copied into four modules.
For M40_bogus_pricematch and M76_ambiguous_subscription_cancel that heuristic
pins a run captured BEFORE per-step world capture existed: it has no world trail,
so "correct step N and drive forward" silently resumes from the run's FINAL world
(every later step's effects already applied) and a fork has no checkpoint to
restore from. Meanwhile a newer, complete run sits in the same table, ignored.

These tests hold the two properties that fix it: an explicit binding is the
source of truth, and the fallback prefers a run that can actually be forked from.
"""

import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from app import canonical, models

sys.path.insert(0, str(Path(__file__).parent))
from test_gym import _synthetic_gym_review  # noqa: E402


def _run(task_id: str, *, success: bool = False, steps: int = 3, world: bool = False):
    """A (run, review) pair shaped like the gym harness produces.

    `world=False` reproduces a capture from before gym commit 2564d7e ("record the
    full multi-app world after every step"): every step is present, no step has a
    world_after, and `gymResume.worldTrail` is therefore a list of Nones.
    """
    run, review = _synthetic_gym_review(task_id, success=success)
    run["trajectory"]["steps"] = run["trajectory"]["steps"][:steps]
    review["steps"] = review["steps"][:steps]
    for i, st in enumerate(run["trajectory"]["steps"]):
        st["world_after"] = {"shop": {"clock": i + 1}} if world else None
    review["gymResume"]["worldTrail"] = [st["world_after"] for st in run["trajectory"]["steps"]]
    return run, review


def _persist(db, task_id: str, *, world: bool, steps: int = 3, success: bool = False,
             raw: bool = True, at: int | None = None, brief: str | None = None) -> models.Trajectory:
    """Persist a run and stamp WHEN it was captured.

    `at` is not decoration. Real captures are minutes apart, but a test inserts
    them in the same second, and SQLite's CURRENT_TIMESTAMP has one-second
    resolution — so without an explicit timestamp the ordering these tests are
    about would be decided by a random UUID tiebreak.
    """
    from datetime import datetime
    from app.api.gym import _persist_gym_review

    run, review = _run(task_id, success=success, steps=steps, world=world)
    sid = _persist_gym_review(db, task_id, "openai", run, review, persist_raw=raw, brief=brief)
    traj = db.scalar(select(models.Trajectory).where(models.Trajectory.session_id == UUID(sid)))
    if at is not None:
        traj.created_at = datetime(2026, 7, 20, 12, at, 0)
        db.flush()
    return traj


def _task(db, external_id: str) -> models.Task:
    return db.scalar(select(models.Task).where(models.Task.external_id == external_id))


def _oldest_with_raw(db, task_id: UUID) -> models.Trajectory | None:
    """The heuristic this module replaces, kept here so the regression it caused
    stays visible: any test that asserts "not this one" is asserting the fix."""
    rows = db.scalars(
        select(models.Trajectory)
        .join(models.ReviewSession, models.Trajectory.session_id == models.ReviewSession.id)
        .where(models.ReviewSession.task_id == task_id, models.Trajectory.source == "gym")
        .order_by(models.Trajectory.created_at.asc())
        .limit(50)
    ).all()
    return next((t for t in rows if t.raw), None)


def _reviewer(db, email: str = "canon-reviewer@deccan.ai", role: str = "reviewer") -> models.Annotator:
    a = models.Annotator(email=email, role=role)
    db.add(a)
    db.flush()
    return a


# --------------------------------------------------------------------------- the bug
def test_the_earliest_full_run_stays_canonical_even_when_a_newer_one_is_forkable(db_session):
    """THE M40/M76 SHAPE, and the trap in it. The first capture predates per-step
    world capture, so nothing can be forked from it. Ranking forkability first
    looks like the fix and is a silent data-integrity bug: EVERY curated breaker
    is un-forkable, so any later run — including an annotator's prompt-edit
    re-run — would outrank the very breaker under review and become what every
    annotator opens. Canonical is an IDENTITY question ("which run is this task's
    recorded failure"), not a quality one. The missing world trail is real, and it
    is fixed by backfilling THAT run, not by selecting a different one."""
    tid = "M40/bogus_pricematch"
    old = _persist(db_session, tid, world=False, at=1)
    new = _persist(db_session, tid, world=True, at=2)
    db_session.commit()
    task = _task(db_session, tid)

    assert canonical.for_task(db_session, task.id).id == old.id
    assert canonical.world_evidence(db_session, old)["worldSteps"] == 0
    assert canonical.world_evidence(db_session, new)["worldSteps"] == 3


def test_a_prompt_edit_rerun_never_becomes_canonical(db_session):
    """It answers a DIFFERENT prompt, so whatever it shows is not this task's
    recorded failure. Nothing else on the row distinguishes it from an original,
    which is why the payload is stamped when it is persisted."""
    from app.api.gym import _persist_gym_review

    tid = "M40/bogus_pricematch"
    original = _persist(db_session, tid, world=False, at=1)
    run, review = _run(tid, steps=5, world=True)
    _persist_gym_review(db_session, tid, "openai", run, review, brief="reworded prompt")
    db_session.commit()
    task = _task(db_session, tid)

    assert canonical.for_task(db_session, task.id).id == original.id
    ids = [t.id for t, _ in canonical.candidates(db_session, task.id)]
    assert len(ids) == 1 and ids[0] == original.id, "a prompt-edit run is not a candidate at all"


def test_binding_is_how_a_different_run_becomes_canonical(db_session):
    """Deliberately, on the record — never as a side effect of somebody re-running."""
    tid = "M40/bogus_pricematch"
    _persist(db_session, tid, world=False, at=1)
    better = _persist(db_session, tid, world=True, at=2)
    db_session.commit()
    task = _task(db_session, tid)

    canonical.bind(db_session, task=task, trajectory=better,
                   actor=_reviewer(db_session), reason="backfilled world trail")
    db_session.commit()
    assert canonical.for_task(db_session, task.id).id == better.id


def test_a_canonical_run_without_a_world_trail_is_reported_not_routed_around(db_session):
    """A fork with no checkpoint does not fail — replay.restore_and_replay skips
    restoration entirely and reports success against whatever state it finds. The
    audit is the only place that gap is visible before it ships, so it must stay
    visible rather than being papered over by picking a different run."""
    tid = "M76/ambiguous_subscription_cancel"
    _persist(db_session, tid, world=False, at=1)
    db_session.commit()

    row = canonical.audit(db_session)["tasks"][0]
    assert row["taskId"] == tid
    assert row["forkable"] is False
    assert row["correctable"] is False, "no better candidate exists yet — do not imply one"

    _persist(db_session, tid, world=True, at=2)
    db_session.commit()
    row = canonical.audit(db_session)["tasks"][0]
    assert row["forkable"] is False, "canonical did not move — the gap is still real"
    assert row["correctable"] is True, "and now a better run exists to BIND, deliberately"



# --------------------------------------------------------------------------- binding wins
def test_an_explicit_binding_beats_every_heuristic(db_session):
    """A person decided; nothing derived from timestamps or payload shape may
    override that. This is how M40 and M76 get corrected."""
    tid = "M41/ambiguous_return"
    first = _persist(db_session, tid, world=True)
    second = _persist(db_session, tid, world=True, steps=2)
    db_session.commit()
    task = _task(db_session, tid)
    assert canonical.for_task(db_session, task.id).id == first.id  # unbound: oldest usable

    canonical.bind(db_session, task=task, trajectory=second, actor=_reviewer(db_session), reason="re-captured after the wipe")
    db_session.commit()
    assert canonical.for_task(db_session, task.id).id == second.id


def test_rebinding_replaces_the_decision_and_leaves_an_audit_trail(db_session):
    """This rewrites what every annotator opening the task sees, so who did it and
    why must be recoverable afterwards."""
    tid = "M47/phantom_duplicate"
    a = _persist(db_session, tid, world=True)
    b = _persist(db_session, tid, world=True, steps=2)
    db_session.commit()
    task = _task(db_session, tid)
    rev = _reviewer(db_session)

    canonical.bind(db_session, task=task, trajectory=a, actor=rev, reason="first")
    canonical.bind(db_session, task=task, trajectory=b, actor=rev, reason="second")
    db_session.commit()

    rows = db_session.scalars(select(canonical.CanonicalRun).where(canonical.CanonicalRun.task_id == task.id)).all()
    assert len(rows) == 1 and rows[0].trajectory_id == b.id, "one binding per task, updated in place"
    logs = db_session.scalars(
        select(models.AuditLog).where(models.AuditLog.action == "canonical.bind")
    ).all()
    assert [log.meta["reason"] for log in logs] == ["first", "second"]
    assert all(log.actor == rev.email for log in logs)


def test_only_a_reviewer_or_admin_may_bind(db_session):
    """Binding is a dataset-wide edit disguised as a one-row write."""
    tid = "M57/birthday_errand"
    t = _persist(db_session, tid, world=True)
    db_session.commit()
    task = _task(db_session, tid)

    with pytest.raises(canonical.NotPermitted):
        canonical.bind(db_session, task=task, trajectory=t, actor=_reviewer(db_session, "hand@x.io", "annotator"))
    canonical.bind(db_session, task=task, trajectory=t, actor=_reviewer(db_session, "boss@x.io", "admin"))
    db_session.commit()
    assert canonical.for_task(db_session, task.id).id == t.id


def test_binding_refuses_a_run_that_cannot_serve_as_canonical(db_session):
    """persisted-review replays the bound payload; a run from another task, or one
    persisted without a payload (every drive-forward continuation), would 404 for
    every annotator instead of opening the breaker."""
    mine = _persist(db_session, "M59/injection_exfil", world=True)
    theirs = _persist(db_session, "M70/mixed_basket", world=True)
    cont = _persist(db_session, "M59/injection_exfil", world=True, raw=False)
    db_session.commit()
    task = _task(db_session, "M59/injection_exfil")
    rev = _reviewer(db_session)

    with pytest.raises(canonical.BindingRefused):
        canonical.bind(db_session, task=task, trajectory=theirs, actor=rev)
    with pytest.raises(canonical.BindingRefused):
        canonical.bind(db_session, task=task, trajectory=cont, actor=rev)
    canonical.bind(db_session, task=task, trajectory=mine, actor=rev)


# --------------------------------------------------------------------------- the fallback
def test_a_payload_less_run_is_never_canonical_however_many_there_are(db_session):
    """Drive-forward continuations and per-attempt baseline clones both persist
    with raw=None and both match the task+source filter. The old query took the
    50 OLDEST rows and only then looked for a payload, so once 50 of them predated
    the real run the endpoint 404'd on a task that plainly had one."""
    tid = "M75/stale_gift_message"
    for _ in range(60):
        _persist(db_session, tid, world=True, raw=False)
    real = _persist(db_session, tid, world=True)
    db_session.commit()
    task = _task(db_session, tid)

    assert _oldest_with_raw(db_session, task.id) is None, "the old query lost the run behind 50 payload-less rows"
    assert canonical.for_task(db_session, task.id).id == real.id


def test_resolution_is_stable_when_two_runs_share_a_clock_tick(db_session):
    """SQLite's CURRENT_TIMESTAMP has one-second resolution and a re-run seconds
    after the original is routine, so created_at alone cannot separate two runs.
    The answer must not depend on whatever order the storage engine returns."""
    tid = "M37/false_overcharge"
    long_run = _persist(db_session, tid, world=False, steps=3)
    _persist(db_session, tid, world=False, steps=1)
    db_session.commit()
    task = _task(db_session, tid)

    picked = {canonical.for_task(db_session, task.id).id for _ in range(5)}
    assert picked == {long_run.id}, "same inputs, same answer, every call"


def test_the_task_canonical_does_not_move_under_an_attempt_already_baselined(db_session):
    """v1.base_trajectory_id has existed for exactly this and was never read back.
    Re-binding a task must not swap the run out from under a half-finished review."""
    from app import versions

    tid = "M39/phantom_replacement"
    first = _persist(db_session, tid, world=True)
    second = _persist(db_session, tid, world=True, steps=2)
    db_session.commit()
    task = _task(db_session, tid)

    attempt = models.ReviewSession(task_id=task.id, source="gym")
    db_session.add(attempt)
    db_session.flush()
    versions.ensure_root(db_session, attempt, canonical.for_task(db_session, task.id))
    canonical.bind(db_session, task=task, trajectory=second, actor=_reviewer(db_session))
    db_session.commit()

    assert canonical.for_task(db_session, task.id).id == second.id
    assert canonical.for_attempt(db_session, attempt).id == first.id, "the baselined attempt keeps its base"


def test_an_unbaselined_attempt_follows_the_task_binding(db_session):
    tid = "M40/bogus_pricematch_attempt"
    original = _persist(db_session, tid, world=False, at=1)
    complete = _persist(db_session, tid, world=True, at=2)
    db_session.commit()
    task = _task(db_session, tid)

    attempt = models.ReviewSession(task_id=task.id, source="gym")
    db_session.add(attempt)
    db_session.commit()
    assert canonical.for_attempt(db_session, attempt).id == original.id

    # …and follows the binding once a reviewer makes one, which is the only way
    # the served run ever changes.
    canonical.bind(db_session, task=task, trajectory=complete,
                   actor=_reviewer(db_session), reason="backfilled")
    db_session.commit()
    assert canonical.for_attempt(db_session, attempt).id == complete.id


def test_a_task_with_no_recorded_run_resolves_to_nothing(db_session):
    task = models.Task(external_id="M00/never_run", title="t", prompt="p", source="gym")
    db_session.add(task)
    db_session.commit()
    assert canonical.for_task(db_session, task.id) is None
    row = canonical.audit(db_session)["tasks"][0]
    assert row["candidates"] == 0 and row["canonicalTrajectoryId"] is None


# --------------------------------------------------------------------------- the audit
def test_the_audit_measures_world_coverage_rather_than_assuming_it(db_session):
    """§8.7: data availability is a hypothesis to be measured. The summary counts
    only the tasks actually scanned, so nobody can read it as dataset-wide coverage."""
    _persist(db_session, "M01/complete", world=True, at=1)
    _persist(db_session, "M02/worldless", world=False, at=1)
    _persist(db_session, "M03/correctable", world=False, at=1)
    _persist(db_session, "M03/correctable", world=True, at=2)
    db_session.commit()

    out = canonical.audit(db_session)
    by_id = {r["taskId"]: r for r in out["tasks"]}
    assert by_id["M01/complete"]["forkable"] is True
    assert by_id["M02/worldless"]["forkable"] is False, "and nothing better exists to bind"
    assert by_id["M02/worldless"]["correctable"] is False
    # The M40/M76 shape: canonical stays the original and stays un-forkable, but a
    # complete run exists — so the audit says a bind would fix it, rather than
    # silently having swapped the run out from under everyone.
    assert by_id["M03/correctable"]["forkable"] is False
    assert by_id["M03/correctable"]["correctable"] is True
    assert out["summary"] == {"tasks": 3, "bound": 0, "withCanonical": 3, "forkable": 1, "unforkable": 2,
                              "correctable": 1, "noRun": 0}


def test_the_audit_flags_a_binding_that_pins_an_unforkable_run(db_session):
    """A human can bind a world-less run — deliberately or by mistake. The preflight
    must report it as correctable rather than trusting the binding to be right."""
    tid = "M04/bound_badly"
    worldless = _persist(db_session, tid, world=False)
    _persist(db_session, tid, world=True)
    db_session.commit()
    task = _task(db_session, tid)
    canonical.bind(db_session, task=task, trajectory=worldless, actor=_reviewer(db_session), reason="pinned by hand")
    db_session.commit()

    row = canonical.audit(db_session)["tasks"][0]
    assert row["bound"] is True and row["forkable"] is False
    assert row["correctable"] is True and row["bestCandidateWorldSteps"] == 3


# --------------------------------------------------------------------------- through the API
def test_reopening_a_task_serves_the_bound_run(client, db_session):
    """The end-to-end shape of the M40 fix: the annotator opening the breaker gets
    the run somebody bound, with the world trail the UI needs to resume a
    correction from step N instead of from the run's final state."""
    tid = "M40/api_binding"
    _persist(db_session, tid, world=False, steps=3, at=1)
    complete = _persist(db_session, tid, world=True, steps=2, success=True, at=2)
    db_session.commit()

    body = client.get(f"/api/gym/tasks/{tid}/persisted-review").json()
    assert len(body["steps"]) == 3, "until somebody binds, the ORIGINAL run is what the task is"
    assert not [w for w in body["gymResume"]["worldTrail"] if w], (
        "and its missing world trail stays visible rather than being routed around"
    )

    task = _task(db_session, tid)
    canonical.bind(db_session, task=task, trajectory=complete, actor=_reviewer(db_session), reason="M40 correction")
    db_session.commit()
    after = client.get(f"/api/gym/tasks/{tid}/persisted-review").json()
    assert len(after["steps"]) == 2 and after["replayed"] is True


def test_the_audit_survives_a_binder_who_no_longer_exists(db_session):
    """The audit is the one report that tells anyone the dataset has a gap. A
    dangling annotator id must not take the whole thing down with an
    AttributeError."""
    tid = "M05/orphan_binder"
    traj = _persist(db_session, tid, world=True, at=1)
    task = _task(db_session, tid)
    canonical.bind(db_session, task=task, trajectory=traj, actor=_reviewer(db_session, "gone@deccan.ai"))
    db_session.commit()

    binding = canonical.bound_run(db_session, task.id)
    binding.bound_by_id = UUID("00000000-0000-0000-0000-0000000000ff")  # purged reviewer
    db_session.commit()

    row = next(r for r in canonical.audit(db_session)["tasks"] if r["taskId"] == tid)
    assert row["bound"] is True and row["boundBy"] is None


def test_a_persisted_gym_run_carries_replayable_locators(db_session):
    """Without a locator a step cannot be replayed, and finalize refuses the whole
    trajectory rather than replaying a shortened one — so a canonical run with no
    locators can never ship. That is EVERY breaker in the set.

    Found by a full-platform smoke test, not by the suite: the same omission was
    fixed once in agent_runs.complete() and missed here, in the path every
    canonical run actually goes through."""
    tid = "M40/locator_check"
    traj = _persist(db_session, tid, world=True, at=1)
    db_session.commit()

    steps = db_session.scalars(
        select(models.TrajectoryStep).where(models.TrajectoryStep.trajectory_id == traj.id)
    ).all()
    assert steps, "the fixture persisted no steps"
    clickish = [s for s in steps if s.action_type not in ("navigate", "press", "wait")]
    assert clickish, "the fixture has no locator-requiring steps to check"
    assert all(s.semantic_locator for s in clickish), (
        "a persisted gym step must carry a semantic locator or it is unreplayable"
    )


def test_bind_best_picks_the_most_complete_run_not_the_earliest(db_session):
    """`candidates` is ordered for canonical IDENTITY — earliest wins — so taking
    the first forkable one hands back the OLDEST usable run, which on a task being
    repaired is likely the thin one that needed repairing. Measured live: --best
    chose a 0/3 world trail over a 6/6. An operator typing --best is asking for
    the run with the most evidence."""
    from app.canonical import _forkable, candidates

    tid = "M40/best_pick"
    # The synthetic builder tops out at 3 steps, so ask for 1 vs 3 — a fixture
    # whose two runs are actually identical would let this pass on a tie.
    _persist(db_session, tid, world=True, steps=1, at=1)   # forkable but thin
    rich = _persist(db_session, tid, world=True, steps=3, at=2)  # the complete one
    db_session.commit()
    task = _task(db_session, tid)

    usable = [(t, e) for t, e in candidates(db_session, task.id) if _forkable(e)]
    first_forkable = usable[0][0]
    most_complete = max(usable, key=lambda p: (p[1]["worldSteps"], p[1]["trail"], p[1]["steps"]))[0]

    assert first_forkable.id != rich.id, "the earliest-first ordering is what makes this a trap"
    assert most_complete.id == rich.id, "--best must choose the run with the most evidence"
