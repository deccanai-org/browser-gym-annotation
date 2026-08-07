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
    assert sample["schema"] == "golden-sample/5"
    # /4 pairs every action with what could be SEEN when it was taken. Both keys
    # must be present on every step even where this fixture has no artifacts:
    # a missing key is indistinguishable from "never captured", and a consumer
    # reading the schema version has to be able to rely on the shape.
    for st in sample["golden_trajectory"]:
        assert "observation" in st and "screenshot" in st
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
    task.prompt = "a different task entirely"
    task.seed = s.seed + 7
    db_session.query(models.Verifier).filter(models.Verifier.suite_id == suite.id).delete()
    db_session.commit()

    after = build_sample(db_session, s)
    assert after["golden_trajectory"] == before["golden_trajectory"]
    assert after["verifiers"] == before["verifiers"] != []
    # The recorded run and the task block were rebuilt LIVE on this path even
    # though the docstring promised a shipped sample cannot drift, so a
    # re-capture or a catalog reseed rewrote what a delivered bundle said the
    # annotator was asked to do and what the agent actually did.
    assert after["recorded_trajectory"] == before["recorded_trajectory"] != []
    assert after["task"] == before["task"]
    assert after["task"]["prompt"] == "p"


def test_the_shipped_seed_is_the_one_the_golden_was_recorded_under(db_session, setup):
    """The bundle shipped `task.seed`, the task's CURRENT mutable seed. A client
    resetting at it gets a different world from the one the golden was recorded
    and scored in — the seed has to come from the attempt."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    s.seed = 11
    task.seed = 0
    db_session.flush()
    _approve(db_session, v1)
    ex = FakeExecutor()
    gym = FakeGym(ex)
    _finalize(db_session, setup, ex=ex, gym=gym)
    db_session.commit()

    assert gym.resets == [(task.external_id, 11)], "the replay ran at the attempt's seed"
    assert build_sample(db_session, s)["task"]["seed"] == 11


def test_the_environment_digest_is_pinned_or_says_it_is_unknown(db_session, setup):
    """`environment_image_digest` was hardcoded to "" on every bundle, so the one
    field that pins the build a trajectory was recorded against silently claimed
    the same (blank) environment for all of them."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    v1.environment_image_digest = "sha256:abc123"
    db_session.flush()
    _approve(db_session, v1)
    _finalize(db_session, setup)
    db_session.commit()

    tv = build_sample(db_session, s)["trajectory_version"]
    assert tv["environment_image_digest"] == "sha256:abc123"
    assert tv["environment_image_digest_provenance"]["source"] == "trajectory_version"
    assert tv["environment_image_digest_provenance"]["missing_reason"] is None


def test_an_unknown_environment_digest_is_null_not_an_empty_string(db_session, setup):
    """Nothing stamps a digest on a version recorded outside a provisioned
    workspace. "" reads as a value; a consumer pinning the build has to be able
    to tell "unknown" from "known and blank"."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    _finalize(db_session, setup)
    db_session.commit()

    tv = build_sample(db_session, s)["trajectory_version"]
    assert tv["environment_image_digest"] is None
    assert tv["environment_image_digest_provenance"]["missing_reason"]


def test_the_versioned_bundle_ships_the_per_verifier_outcomes(db_session, setup):
    """The bundle carried a bare 0/1 and nothing about which check produced it,
    which is the only actionable part of a breaker sample."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    _approve(db_session, v1)
    _finalize(db_session, setup, scorer=FakeScorer(reward=0, results={"m0": "fail"}),
              accept_failing=True)
    db_session.commit()

    sample = build_sample(db_session, s)
    assert sample["reward"] == 0
    assert sample["verifier_results"] == {"m0": "fail"}
    assert [(v["id"], v["result"]) for v in sample["verifiers"]] == [("m0", "fail")]


def test_a_versioned_sample_with_no_seed_world_says_so(db_session, setup):
    """The triplet's first leg degraded to the non-world keys of seed_state, so a
    bundle with no world at all shipped {"initial_url", "category", "difficulty"}
    in the slot a client resets from."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    task.seed_state = {"initial_url": "/shop", "category": "e-commerce"}
    db_session.flush()
    _approve(db_session, v1)
    _finalize(db_session, setup)
    db_session.commit()

    sample = build_sample(db_session, s)
    assert sample["initial_state"] is None
    assert sample["initial_state_provenance"]["missing_reason"]
    assert sample["initial_state_provenance"]["metadata"]["initial_url"] == "/shop"

    task.seed_state = {"initial_url": "/shop", "world": {"shop": {"cart": []}}}
    db_session.commit()
    sample = build_sample(db_session, s)
    assert sample["initial_state"] == {"shop": {"cart": []}}
    assert sample["initial_state_provenance"]["source"] == "task.seed_state.world"


def test_a_legacy_submission_still_exports_on_the_old_schema(db_session, setup):
    """The version-bound path must not break samples submitted before it existed —
    but the thin shape has to NAME itself. It used to be the one bundle with no
    `schema` key at all, so a loader reading the dataset could not tell a legacy
    row from a full one except by probing for keys that are legitimately absent
    on both."""
    from app.api.export import build_sample

    s, v1, suite, steps, task = setup
    db_session.add(models.Submission(session_id=s.id, reward=1, kind="golden", snapshot={"verifiers": [], "reward": 1}))
    db_session.commit()
    sample = build_sample(db_session, s)
    assert sample["schema"] == "golden-sample-legacy/1"
    assert "trajectory_version" not in sample
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


def test_a_tab_switch_does_not_block_finalization():
    """A tab switch names an APP, not a page element, so it has no semantic
    locator by nature. Before `_NO_LOCATOR` covered it, every multi-app
    trajectory — the whole point of the cross-app breakers — was refused at the
    last gate for "this step has no semantic locator"."""
    ok, missing = finalize.replayable([
        {"kind": "click", "locator": {"testId": "buy"}, "args": {}},
        {"kind": "switch_tab", "locator": {}, "args": {"app": "mail"}},
        {"kind": "navigate", "locator": {}, "args": {"url": "/cart"}},
    ])
    assert ok and missing == []


def test_a_click_without_a_locator_is_still_refused():
    """The guard on the above: widening the exemption must not make everything
    replayable-by-assertion."""
    ok, missing = finalize.replayable([
        {"kind": "click", "locator": {}, "args": {}},
    ])
    assert not ok and missing == [0]


# --------------------------------------------------------- prepare-ship gates

def test_the_blockers_and_the_real_gates_agree(db_session, setup):
    """`gate_report` lists every unmet gate; `finalize` raises on the first. They
    read the same predicates deliberately — a second hand-maintained copy of
    "what blocks a ship" would drift, and the drift shows up as a button that is
    enabled and then refuses."""
    attempt, version, suite, _steps, _task = setup
    version.status = "candidate"          # not approved
    db_session.commit()

    codes = [b["code"] for b in finalize.gate_report(db_session, attempt, version, suite)]
    assert "not_approved" in codes
    # and finalize refuses for exactly that reason
    with pytest.raises(finalize.NotApproved):
        _finalize(db_session, setup)


def test_an_approved_ready_attempt_reports_no_blockers(db_session, setup):
    attempt, version, suite, _steps, _task = setup
    _approve(db_session, version)
    db_session.commit()
    assert finalize.gate_report(db_session, attempt, version, suite) == []


def test_an_attempt_with_no_verifiers_is_blocked_before_it_wastes_a_replay(db_session, setup):
    """Shipping needs a suite. Saying so up front beats discovering it after a
    full clean-reset replay has run."""
    attempt, version, _suite, _steps, _task = setup
    codes = [b["code"] for b in finalize.gate_report(db_session, attempt, version, None)]
    assert "no_verifiers" in codes


def test_every_blocker_says_what_to_go_and_do(db_session, setup):
    """A code alone is not actionable. The message is the product."""
    attempt, version, _suite, _steps, _task = setup
    version.status = "candidate"
    db_session.commit()
    for b in finalize.gate_report(db_session, attempt, version, None):
        assert b["message"] and len(b["message"]) > 20, b


def test_the_suite_is_derived_from_the_gyms_own_milestone_key(db_session, monkeypatch):
    """The gym calls them `all_milestones` and the bridge passes the verdict
    through unchanged. Reading `milestones` found nothing, so the fallback
    silently never fired and every attempt still reported "no verifier suite" —
    a miss no unit test caught, because only a real gym answers this call.

    Also locks the forbidden inversion: a milestone that must NOT fire passes by
    not firing, so its verdict cannot be read off `fired_at_step` directly.
    """
    from app import gym_review

    verdict = {
        "score": 0.0, "success": False,
        "all_milestones": [
            {"name": "ordered_both_gifts_clean", "weight": 1.0, "fired_at_step": -1,
             "required": True, "forbidden": False},
            {"name": "committed_any_harm", "weight": 0.0, "fired_at_step": -1,
             "required": False, "forbidden": True},
        ],
    }
    got = list(verdict.get("all_milestones") or verdict.get("milestones") or [])
    assert len(got) == 2, "the gym's own key must be the one read"
    assert gym_review._level(got[0]) == "backend"
    assert gym_review._level(got[1]) == "safety", "a forbidden milestone is a safety check"
    assert gym_review._milestone_result(got[0]) == "fail", "required and never fired"
    assert gym_review._milestone_result(got[1]) == "pass", "forbidden and never fired"


def test_an_observation_is_found_on_the_event_when_the_checkpoint_never_got_it(db_session):
    """A bundle shipped 0 of 14 observations for a trajectory that captured all of them.

    The write path only reaches the checkpoint when an observation arrives AFTER
    its event has already become a step. It normally arrives before — the pane
    posts observations alongside the interactions and folding happens server-side
    once the batch lands — so the artifact is written, its id goes on the event
    payload, and the checkpoint link is never made. The reader looked only at the
    checkpoint. Measured on two real attempts: 0/14 and 1/12 reachable before,
    10/14 and 9/12 after.
    """
    from app import checkpoints, finalize

    task = models.Task(external_id=f"OBS-{uuid4().hex[:6]}", title="t", prompt="p", source="gym", seed=0)
    db_session.add(task); db_session.flush()
    s = models.ReviewSession(task_id=task.id, seed=0, status="draft", source="gym")
    db_session.add(s); db_session.flush()
    traj = models.Trajectory(session_id=s.id, source="gym")
    db_session.add(traj); db_session.flush()
    step = models.TrajectoryStep(trajectory_id=traj.id, idx=0, action_type="click",
                                 description="click Send", actor="human")
    db_session.add(step); db_session.flush()

    art = checkpoints.add_artifact(
        db_session, kind="observation", uri=f"attempt/{s.id}/e0.json",
        data=b'{"url":"http://shop/","elements":[]}',
        meta={"url": "http://shop/", "elements": 7, "truncated": False},
    )
    db_session.add(models.InteractionEvent(
        attempt_id=s.id, seq=1, client_event_id="e0", kind="mouseUp",
        payload={"observationArtifactId": str(art.id)}, committed_step_id=step.id,
    ))
    db_session.flush()

    # The step has NO after_checkpoint_id at all — exactly the shape that shipped blank.
    assert step.after_checkpoint_id is None
    ref = finalize._observation_ref(db_session, step)

    assert ref is not None, "an observation that was captured must reach the bundle"
    assert ref["sha256"] == art.sha256
    assert ref["elements"] == 7
    assert ref["url"] == "http://shop/"


def test_a_step_that_truly_has_no_observation_still_says_so(db_session):
    """The fallback must not invent one — a step with nothing captured reports
    null, which is what lets a consumer tell 'not observed' from 'observed and
    empty'."""
    from app import finalize

    task = models.Task(external_id=f"OBS-{uuid4().hex[:6]}", title="t", prompt="p", source="gym", seed=0)
    db_session.add(task); db_session.flush()
    s = models.ReviewSession(task_id=task.id, seed=0, status="draft", source="gym")
    db_session.add(s); db_session.flush()
    traj = models.Trajectory(session_id=s.id, source="gym")
    db_session.add(traj); db_session.flush()
    step = models.TrajectoryStep(trajectory_id=traj.id, idx=0, action_type="click",
                                 description="click", actor="human")
    db_session.add(step); db_session.flush()

    assert finalize._observation_ref(db_session, step) is None
