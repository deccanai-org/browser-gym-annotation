"""Finalization: the approval + clean-replay gate, and the bindings that make a
score mean something."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app import checkpoints, finalize, models, replay, versions


class FakeExecutor:
    def __init__(self, present=("a", "b"), world=None):
        self.present = set(present)
        self._world = world or {"cart": [], "step": 0}
        self.performed: list[str] = []

    def act(self, kind, locator, args):
        if kind == "navigate":
            self.performed.append("navigate")
            return {"ok": True, "resolved": {}}
        t = (locator or {}).get("testId") or ""
        if t not in self.present:
            return {"ok": False, "error": "no element matched the locator"}
        self.performed.append(t)
        self._world["step"] += 1
        return {"ok": True, "resolved": {"selector": f'[data-test-id="{t}"]'}}

    def world(self):
        return dict(self._world)


class FakeGym:
    def __init__(self, ex, resettable=True):
        self.ex, self.resettable, self.resets = ex, resettable, []

    def reset(self, task_id, seed):
        if not self.resettable:
            return None
        self.resets.append((task_id, seed))
        self.ex._world = {"cart": [], "step": 0}
        return {"ok": True}

    def world(self):
        return self.ex.world()


class FakeScorer:
    def __init__(self, reward=1, results=None):
        self.reward, self.results = reward, results or {"m0": "pass"}
        self.scored_world = None

    def score(self, suite, world):
        self.scored_world = world
        return self.reward, self.results


@pytest.fixture()
def setup(db_session):
    task = models.Task(external_id=f"FZ-{uuid4().hex[:6]}", title="t", prompt="p", source="gym", seed=0)
    ann = models.Annotator(email=f"fz-{uuid4().hex[:6]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym", task_revision=3)
    db_session.add(s)
    db_session.flush()
    traj = models.Trajectory(session_id=s.id, agent="gpt-5.5", source="gym")
    suite = models.VerifierSuite(session_id=s.id, version=2)
    db_session.add_all([traj, suite])
    db_session.flush()
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="m0", level="backend", assertion="cart is empty", code=""))

    v1 = versions.create_root(db_session, attempt_id=s.id, base_trajectory_id=traj.id, producer="gpt-5.5")
    steps = []
    for i, t in enumerate(["a", "b"]):
        st = models.TrajectoryStep(
            trajectory_id=traj.id, idx=i, action_type="click", description=f"click {t}",
            actor="agent", semantic_locator={"testId": t},
        )
        db_session.add(st)
        steps.append(st)
    db_session.flush()
    versions.adopt_steps(db_session, v1, steps)
    db_session.commit()
    return s, v1, suite, steps, task


def _approve(db, v):
    versions.set_status(db, v, "approved", expected_revision=v.revision)


def _finalize(db, setup, ex=None, gym=None, scorer=None, **kw):
    s, v1, suite, steps, task = setup
    ex = ex or FakeExecutor()
    return finalize.finalize(
        db, attempt=s, version=v1, suite=suite, executor=ex,
        gym=gym or FakeGym(ex), scorer=scorer or FakeScorer(),
        task_external_id=task.external_id, **kw,
    )


# --------------------------------------------------------------------------- gates
def test_an_unapproved_version_cannot_be_finalized(db_session, setup):
    """The post-submission `accepted` flag is too late to be the gate."""
    with pytest.raises(finalize.NotApproved):
        _finalize(db_session, setup)
    assert db_session.query(models.Submission).count() == 0


def test_an_approved_version_that_does_not_replay_is_refused(db_session, setup):
    """Approval alone would ship a trajectory nobody can reproduce."""
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    with pytest.raises(replay.ReplayRejected):
        _finalize(db_session, setup, ex=FakeExecutor(present=("a",)))  # 'b' is gone
    assert db_session.query(models.Submission).count() == 0


def test_a_step_without_a_locator_is_refused_rather_than_skipped(db_session, setup):
    """A shortened replay would 'pass' by doing less."""
    s, v1, suite, steps, task = setup
    steps[1].semantic_locator = {}
    db_session.flush()
    _approve(db_session, v1)
    with pytest.raises(replay.ReplayRejected) as ei:
        _finalize(db_session, setup)
    assert ei.value.at == 1 and "locator" in ei.value.reason


def test_a_failed_reset_stops_finalization(db_session, setup):
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    ex = FakeExecutor()
    with pytest.raises(replay.ReplayRejected):
        _finalize(db_session, setup, ex=ex, gym=FakeGym(ex, resettable=False))
    assert ex.performed == []


def test_a_version_from_another_attempt_is_refused(db_session, setup):
    s, v1, suite, steps, task = setup
    other = models.ReviewSession(task_id=s.task_id, annotator_id=s.annotator_id, source="gym")
    db_session.add(other)
    db_session.flush()
    _approve(db_session, v1)
    with pytest.raises(versions.LineageError):
        finalize.finalize(db_session, attempt=other, version=v1, suite=suite,
                          executor=FakeExecutor(), gym=FakeGym(FakeExecutor()),
                          scorer=FakeScorer(), task_external_id=task.external_id)


# --------------------------------------------------------------------------- the happy path
def test_finalization_replays_from_a_clean_reset_not_a_checkpoint(db_session, setup):
    """'It reproduces' must not mean 'it reproduces from the state we saved'."""
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    ex = FakeExecutor()
    gym = FakeGym(ex)
    out = _finalize(db_session, setup, ex=ex, gym=gym)
    assert gym.resets == [(task.external_id, 0)]
    assert ex.performed == ["a", "b"], "the WHOLE trajectory runs, from the start"
    assert out["replayed"] and out["steps"] == 2


def test_the_score_names_exactly_what_produced_it(db_session, setup):
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    out = _finalize(db_session, setup)
    db_session.commit()

    run = db_session.get(models.BenchmarkRun, UUID(out["benchmarkRunId"]))
    assert run.trajectory_version_id == v1.id
    assert run.suite_id == suite.id
    assert run.final_checkpoint_id is not None, "a score without an end state cannot be re-checked"

    sub = db_session.get(models.Submission, UUID(out["submissionId"]))
    assert sub.approved_trajectory_version_id == v1.id
    assert sub.benchmark_run_id == run.id
    assert sub.task_revision == 3, "the sample names the task revision it was annotated against"


def test_the_suite_is_scored_against_the_world_the_replay_ended_in(db_session, setup):
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    scorer = FakeScorer()
    _finalize(db_session, setup, scorer=scorer)
    assert scorer.scored_world == {"cart": [], "step": 2}


def test_the_version_is_published_once_it_ships(db_session, setup):
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    _finalize(db_session, setup)
    assert v1.status == "published"


# --------------------------------------------------------------------------- freezing
def test_the_snapshot_survives_later_edits_to_everything_it_references(db_session, setup):
    """Suites get edited and canonical runs get re-captured after a sample ships.
    The deliverable must say what was actually reviewed and scored."""
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    out = _finalize(db_session, setup)
    db_session.commit()
    frozen = db_session.get(models.Submission, UUID(out["submissionId"])).snapshot

    # someone edits the suite and rewrites a step description afterwards
    db_session.query(models.Verifier).filter(models.Verifier.suite_id == suite.id).delete()
    steps[0].description = "rewritten later"
    db_session.commit()

    assert [v["assertion"] for v in frozen["verifiers"]] == ["cart is empty"]
    assert frozen["golden_trajectory"][0]["description"] == "click a"


def test_the_snapshot_carries_the_lineage_and_per_step_provenance(db_session, setup):
    """A hybrid trajectory has to ship saying which steps were the agent's and
    which the human's — version-level `kind` cannot express that."""
    s, v1, suite, steps, task = setup
    v2 = versions.fork_before(db_session, parent=v1, step=steps[1], producer="human")
    versions.append_step(db_session, v2, trajectory_id=steps[0].trajectory_id, actor="human",
                         action_type="click", description="human fix",
                         semantic_locator={"testId": "b"}, human_intent="the agent picked the wrong item")
    db_session.flush()
    _approve(db_session, v2)

    out = finalize.finalize(
        db_session, attempt=s, version=v2, suite=suite, executor=FakeExecutor(),
        gym=FakeGym(FakeExecutor()), scorer=FakeScorer(), task_external_id=task.external_id,
    )
    db_session.commit()
    frozen = db_session.get(models.Submission, UUID(out["submissionId"])).snapshot

    assert [v["versionNo"] for v in frozen["trajectory_version"]["lineage"]] == [1, 2]
    assert [st["actor"] for st in frozen["golden_trajectory"]] == ["agent", "human"]
    assert frozen["golden_trajectory"][1]["human_intent"] == "the agent picked the wrong item"
    assert frozen["golden_trajectory"][0]["stepId"] == str(steps[0].id), "stable ids ship too"


def test_the_frozen_step_carries_its_world_hash(db_session, setup):
    """Whoever trains on this can check the end state themselves."""
    s, v1, suite, steps, task = setup
    cp = checkpoints.capture(db_session, attempt_id=s.id, world={"cart": ["mug"]})
    steps[1].after_checkpoint_id = cp.id
    db_session.flush()
    _approve(db_session, v1)
    out = _finalize(db_session, setup)
    db_session.commit()

    frozen = db_session.get(models.Submission, UUID(out["submissionId"])).snapshot
    assert frozen["golden_trajectory"][1]["world_hash"] == checkpoints.hash_world({"cart": ["mug"]})
    assert frozen["final_world_hash"] == checkpoints.hash_world({"cart": [], "step": 2})


# --------------------------------------------------------------------------- export
def test_the_exported_sample_ships_the_lineage_and_authorship(db_session, setup):
    """Asserted on the EXPORTED JSON, not the DB rows — that JSON is the product,
    and a passing DB check has hidden a broken export before."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    v2 = versions.fork_before(db_session, parent=v1, step=steps[1], producer="human")
    versions.append_step(db_session, v2, trajectory_id=steps[0].trajectory_id, actor="human",
                         action_type="click", description="human fix",
                         semantic_locator={"testId": "b"}, human_intent="the agent added the wrong item")
    db_session.flush()
    _approve(db_session, v2)
    finalize.finalize(db_session, attempt=s, version=v2, suite=suite, executor=FakeExecutor(),
                      gym=FakeGym(FakeExecutor()), scorer=FakeScorer(reward=1),
                      task_external_id=task.external_id)
    db_session.commit()

    sample = build_sample(db_session, s)
    assert sample["schema"] == "golden-sample/2"
    assert sample["task"]["revision"] == 3
    assert sample["trajectory_version"]["version_no"] == 2
    assert [v["versionNo"] for v in sample["trajectory_version"]["lineage"]] == [1, 2]
    assert [st["actor"] for st in sample["golden_trajectory"]] == ["agent", "human"]
    assert sample["golden_trajectory"][1]["human_intent"] == "the agent added the wrong item"
    assert sample["golden_trajectory"][1]["locator"] == {"testId": "b"}, (
        "a golden without locators is not replayable by whoever receives it"
    )
    assert sample["corrections"] == [{"version_no": 2, "kind": "agent_correction", "producer": "human"}]
    assert sample["reward"] == 1 and sample["final_world_hash"]


def test_the_exported_sample_cannot_drift_after_it_ships(db_session, setup):
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    _finalize(db_session, setup)
    db_session.commit()
    before = build_sample(db_session, s)

    steps[0].description = "rewritten after shipping"
    db_session.query(models.Verifier).filter(models.Verifier.suite_id == suite.id).delete()
    db_session.commit()

    after = build_sample(db_session, s)
    assert after["golden_trajectory"] == before["golden_trajectory"]
    assert after["verifiers"] == before["verifiers"] != []


def test_a_legacy_submission_still_exports_on_the_old_schema(db_session, setup):
    """The version-bound path must not break samples submitted before it existed."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    db_session.add(models.Submission(session_id=s.id, reward=1, kind="golden", snapshot={"verifiers": [], "reward": 1}))
    db_session.commit()
    sample = build_sample(db_session, s)
    assert "schema" not in sample and "trajectory_version" not in sample
    assert sample["reward"] == 1


# --------------------------------------------------------------------------- provenance
def test_the_sample_kind_is_derived_not_asserted_by_the_caller(db_session, setup):
    """The legacy path derives kind server-side precisely so a run that only
    passes because a human overrode a SAFETY verifier ships as `flagged` rather
    than as training gold. The version path took it from the request body, so it
    could never produce `flagged` and a client could label anything `golden` —
    dropping the provenance silently."""
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    # …and shipping a failing run has to be deliberate, so say so.
    with pytest.raises(finalize.NotApproved, match="does not pass"):
        _finalize(db_session, setup, scorer=FakeScorer(reward=0))
    out = _finalize(db_session, setup, scorer=FakeScorer(reward=0), accept_failing=True)
    db_session.commit()
    sub = db_session.get(models.Submission, UUID(out["submissionId"]))
    assert sub.kind == "breaker", "a failing run is not a golden however it is labelled"


def test_a_passing_run_is_golden(db_session, setup):
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    out = _finalize(db_session, setup, scorer=FakeScorer(reward=1))
    db_session.commit()
    assert db_session.get(models.Submission, UUID(out["submissionId"])).kind == "golden"


def test_a_safety_override_flags_the_sample(db_session, setup):
    """The rule exists so an unsafe trajectory cannot ship as something to train
    on. v2 has no override path yet — this pins the rule so adding one cannot
    quietly forget it."""
    from app import finalize as fz

    s, v1, suite, steps, task = setup
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="safe1", level="safety",
                                   assertion="no false refund claim", code=""))
    db_session.commit()
    db_session.refresh(suite)
    assert fz._kind_for(suite, 1, ["safe1"]) == "flagged"
    assert fz._kind_for(suite, 1, ["m0"]) == "golden", "overriding a non-safety check is not a flag"
    assert fz._kind_for(suite, 1, []) == "golden"


def test_a_step_marked_wrong_cannot_ship_inside_the_golden(db_session, setup):
    """The UI promises "steps you rejected are not in it". Forking before a bad
    step is how that normally holds, but a verdict recorded WITHOUT a fork left
    the step in place and nothing downstream looked — so the promise was a lie in
    exactly the case an annotator would trust it most."""
    from app import versions as vmod

    s, v1, suite, steps, task = setup
    vmod.set_verdict(db_session, attempt_id=s.id, step_id=steps[1].id, verdict="rejected",
                     note="wrong product")
    db_session.commit()
    _approve(db_session, v1)

    with pytest.raises(finalize.NotApproved, match="marked wrong"):
        _finalize(db_session, setup)
    assert db_session.query(models.Submission).count() == 0


def test_clearing_the_verdict_or_forking_lets_it_ship(db_session, setup):
    """The refusal has to name a way forward that works, or it is just a wall."""
    from app import versions as vmod

    s, v1, suite, steps, task = setup
    vmod.set_verdict(db_session, attempt_id=s.id, step_id=steps[1].id, verdict="rejected")
    db_session.commit()
    _approve(db_session, v1)
    with pytest.raises(finalize.NotApproved):
        _finalize(db_session, setup)

    vmod.set_verdict(db_session, attempt_id=s.id, step_id=steps[1].id, verdict="pending")
    db_session.commit()
    assert _finalize(db_session, setup)["reward"] == 1


def test_a_submitted_attempt_cannot_be_finalized_again(client, db_session, setup):
    """Every other mutating endpoint asserts this; finalize writes a Submission, a
    BenchmarkRun and a version status, so it has to as well."""
    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    _finalize(db_session, setup)
    s.status = "submitted"
    db_session.commit()

    r = client.post(f"/api/sessions/{s.id}/finalize", json={"versionId": str(v1.id)})
    assert r.status_code in (403, 404, 409), r.text
