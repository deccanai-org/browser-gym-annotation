"""The reward on a shipped sample must come from executing the suite that ships
with it.

It did not. Finalization took the number wholly from the gym's aggregate
`success` flag, so the verifiers frozen into the sample — the annotator's own
assertions, or an adopted autogen suite with real executable IR — contributed
nothing to the reward printed beside them. And the per-verifier detail was read
off `milestones` / `passed`, keys the gym has never emitted (it returns
`all_milestones` with `fired_at_step` + `forbidden`), so every result was only a
copy of that same aggregate flag: on a passing run an optional milestone that
never fired and a forbidden tripwire both recorded as passing.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app import finalize, models, versions
from app.api.versions import SuiteScorer


def _milestone(name, *, fired=-1, required=True, forbidden=False, weight=1.0):
    """One entry of the gym's real verdict — server/verifiers.py
    TaskSuite.evaluate. Note what is NOT here: an `id`, and any verdict string."""
    return {"name": name, "weight": weight, "fired_at_step": fired,
            "required": required, "forbidden": forbidden}


class FakeGym:
    """Holds a world and answers /_harness/verify in the gym's real shape."""

    def __init__(self, world=None, success=True, milestones=()):
        self._world = world if world is not None else {}
        self.verdict = {"score": 1.0 if success else 0.0, "success": success,
                        "newly_fired": [], "missed_milestones": [],
                        "all_milestones": list(milestones)}

    def world(self):
        return dict(self._world)

    def reset(self, task_id, seed):
        return {"ok": True}

    def verify(self, step=0):
        return dict(self.verdict)


class FakeExecutor:
    def act(self, kind, locator, args):
        return {"ok": True, "resolved": {}}


@pytest.fixture()
def attempt(db_session):
    """An approved one-step version, ready to finalize."""
    task = models.Task(external_id=f"RT-{uuid4().hex[:6]}", title="t", prompt="p", source="gym", seed=0)
    ann = models.Annotator(email=f"rt-{uuid4().hex[:6]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym", task_revision=1)
    db_session.add(s)
    db_session.flush()
    traj = models.Trajectory(session_id=s.id, agent="human", source="manual")
    db_session.add(traj)
    db_session.flush()
    v1 = versions.create_root(db_session, attempt_id=s.id, base_trajectory_id=traj.id, producer="human")
    st = models.TrajectoryStep(trajectory_id=traj.id, idx=0, action_type="click", description="click a",
                               actor="human", semantic_locator={"testId": "a"})
    db_session.add(st)
    db_session.flush()
    versions.adopt_steps(db_session, v1, [st])
    versions.set_status(db_session, v1, "approved", expected_revision=v1.revision)
    db_session.commit()
    return s, v1, task


def _suite(db, session_id, specs):
    """specs: (ext_id, check_ir, gym_result) — as write_suite persists them."""
    suite = models.VerifierSuite(session_id=session_id, version=1)
    db.add(suite)
    db.flush()
    for ext, ir, gr in specs:
        db.add(models.Verifier(suite_id=suite.id, ext_id=ext, level="backend",
                               assertion=ext, code="", check_ir=ir or {}, gym_result=gr))
    db.flush()
    db.refresh(suite)
    return suite


# --------------------------------------------------------------------------- the reward
def test_a_suite_that_fails_is_not_rewarded_just_because_the_gym_succeeded(db_session, attempt):
    """The headline case. The bound suite says the cart was not emptied; the gym's
    aggregate says the episode succeeded. Taking the reward from the aggregate
    ships a sample whose own verifiers refute it."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("cart_empty", {"kind": "state_eq", "path": "shop.cart.total", "value": 0}, "")])
    gym = FakeGym(world={"shop": {"cart": {"total": 42}}}, success=True)

    reward, results = SuiteScorer(gym).score(suite, gym.world())

    assert results == {"cart_empty": "fail"}
    assert reward == 0, "the suite that ships with the sample is what scores it"


def test_a_suite_that_passes_is_rewarded(db_session, attempt):
    """The positive control: a check that genuinely holds against the world the
    replay ended in must still reach reward 1."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("cart_empty", {"kind": "state_eq", "path": "shop.cart.total", "value": 0}, "")])
    gym = FakeGym(world={"shop": {"cart": {"total": 0}}}, success=False)

    reward, results = SuiteScorer(gym).score(suite, gym.world())

    assert results == {"cart_empty": "pass"} and reward == 1


def test_an_empty_suite_proves_nothing_and_cannot_score_one(db_session, attempt):
    """A reward of 1 from zero checks is the same lie in its purest form."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [])
    reward, results = SuiteScorer(FakeGym(success=True)).score(suite, {})
    assert (reward, results) == (0, {})


# --------------------------------------------------------------------------- unexecutable
def test_a_verifier_that_cannot_be_executed_is_unknown_never_a_pass(db_session, attempt):
    """A free-text human assertion with no IR, on a run the gym called a success,
    was recorded as `pass` — the aggregate flag copied onto a check nobody ran."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("the_reply_is_polite", {}, "")])
    scorer = SuiteScorer(FakeGym(success=True))

    reward, results = scorer.score(suite, {})

    assert results == {"the_reply_is_polite": "unknown"}
    assert reward == 0
    assert scorer.unproven == ["the_reply_is_polite"] and scorer.executed == []


def test_a_trace_check_is_unknown_rather_than_scored_against_an_empty_trace(db_session, attempt):
    """`trace_max_steps` against the empty trace this scorer has is trivially
    TRUE, so scoring it here would report a pass for a check that never ran."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("under_20_steps", {"kind": "trace_max_steps", "n": 20}, "")])
    reward, results = SuiteScorer(FakeGym(success=True)).score(suite, {})
    assert results == {"under_20_steps": "unknown"} and reward == 0


def test_a_milestone_the_gym_did_not_report_is_unknown(db_session, attempt):
    """A suite bound to a milestone the verdict no longer contains (renamed task,
    unreachable gym) must say so, not inherit the aggregate."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("held_order", {"kind": "gym_milestone", "id": "held_order"}, "pass")])
    gym = FakeGym(success=True, milestones=[_milestone("something_else", fired=1)])
    reward, results = SuiteScorer(gym).score(suite, {})
    assert results == {"held_order": "unknown"} and reward == 0


# --------------------------------------------------------------------------- milestone truth
def test_an_optional_milestone_that_never_fired_is_not_a_pass(db_session, attempt):
    """On a passing run every per-verifier result was a copy of `success`, so a
    milestone that never fired shipped as proof that it had."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [
        ("did_the_task", {"kind": "gym_milestone", "id": "did_the_task"}, ""),
        ("also_wrote_a_note", {"kind": "gym_milestone", "id": "also_wrote_a_note"}, ""),
    ])
    gym = FakeGym(success=True, milestones=[
        _milestone("did_the_task", fired=3),
        _milestone("also_wrote_a_note", fired=-1, required=False, weight=0.0),
    ])

    reward, results = SuiteScorer(gym).score(suite, {})

    assert results == {"did_the_task": "pass", "also_wrote_a_note": "fail"}
    assert reward == 0


def test_a_forbidden_tripwire_is_read_by_firing_not_by_the_aggregate(db_session, attempt):
    """A forbidden milestone passes by NOT firing, and the run's other milestones
    keep their own verdicts. Copying the aggregate got BOTH wrong: it marked the
    tripwire that fired as passing on a success, and the milestones that really
    fired as failing on a failure."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [
        ("did_the_task", {"kind": "gym_milestone", "id": "did_the_task"}, ""),
        ("leaked_the_card", {"kind": "gym_milestone", "id": "leaked_the_card"}, ""),
    ])
    gym = FakeGym(success=False, milestones=[
        _milestone("did_the_task", fired=3),
        _milestone("leaked_the_card", fired=5, required=False, forbidden=True, weight=0.0),
    ])

    _reward, results = SuiteScorer(gym).score(suite, {})

    assert results == {"did_the_task": "pass", "leaked_the_card": "fail"}


def test_a_suite_written_off_the_milestone_list_still_executes(db_session, attempt):
    """prepare-ship writes the gym's own milestones as the suite; an older one
    carries no IR at all, only the milestone NAME as its ext_id. That is still
    executable, and calling it unknown would refuse to ship every such attempt."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("did_the_task", {}, "")])
    gym = FakeGym(success=True, milestones=[_milestone("did_the_task", fired=2)])
    reward, results = SuiteScorer(gym).score(suite, {})
    assert results == {"did_the_task": "pass"} and reward == 1


# --------------------------------------------------------------------------- what ships
def test_the_shipped_sample_carries_the_result_from_the_run_that_scored_it(db_session, attempt):
    """`gym_result` was written once when the suite was created and never
    refreshed, so a sample could ship a reward beside verifiers still asserting a
    verdict from some earlier run."""
    s, v, task = attempt
    suite = _suite(db_session, s.id, [
        ("did_the_task", {"kind": "gym_milestone", "id": "did_the_task"}, "pass"),
        ("kept_the_promo", {"kind": "gym_milestone", "id": "kept_the_promo"}, "pass"),
    ])
    gym = FakeGym(success=False, milestones=[
        _milestone("did_the_task", fired=1),
        _milestone("kept_the_promo", fired=-1),
    ])

    out = finalize.finalize(
        db_session, attempt=s, version=v, suite=suite, executor=FakeExecutor(), gym=gym,
        scorer=SuiteScorer(gym), task_external_id=task.external_id,
        require_replay=False, accept_failing=True,
    )
    db_session.commit()

    sub = db_session.get(models.Submission, UUID(out["submissionId"]))
    shipped = {x["id"]: x["gym_result"] for x in sub.snapshot["verifiers"]}
    assert shipped == {"did_the_task": "pass", "kept_the_promo": "fail"}, \
        "the exported verdicts must be the ones the scoring run produced"
    assert sub.snapshot["reward"] == 0 and sub.kind == "breaker"


def test_the_gym_aggregate_is_kept_beside_the_reward_not_as_it(db_session, attempt):
    """The aggregate is still worth reporting — it just is not the reward."""
    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("cart_empty", {"kind": "state_eq", "path": "cart.total", "value": 0}, "")])
    gym = FakeGym(world={"cart": {"total": 9}}, success=True)
    scorer = SuiteScorer(gym)

    reward, _results = scorer.score(suite, gym.world())

    assert reward == 0 and scorer.gym_success is True


def test_an_unreachable_gym_proves_nothing(db_session, attempt):
    """No verdict at all must not read as a silent pass, and must not claim the
    gym said anything either."""
    class Dead:
        def verify(self, step=0):
            return None

    s, _v, _task = attempt
    suite = _suite(db_session, s.id, [("did_the_task", {"kind": "gym_milestone", "id": "did_the_task"}, "pass")])
    scorer = SuiteScorer(Dead())
    reward, results = scorer.score(suite, {})
    assert results == {"did_the_task": "unknown"} and reward == 0
    assert scorer.gym_success is None
