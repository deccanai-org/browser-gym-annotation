"""Agent branch runs: candidate-only completion, idempotency, and cap accounting."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app import agent_runs, models, versions


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"AR-{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"ar-{uuid4().hex[:6]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.flush()
    t = models.Trajectory(session_id=s.id, agent="gpt-5.5", source="gym")
    db_session.add(t)
    db_session.commit()
    s.trajectory = t
    return s


@pytest.fixture()
def v1(db_session, attempt):
    v = versions.create_root(db_session, attempt_id=attempt.id, base_trajectory_id=attempt.trajectory.id)
    steps = []
    for i in range(3):
        cp = models.EnvironmentCheckpoint(attempt_id=attempt.id, world={"step": i}, step_clock=i)
        db_session.add(cp)
        db_session.flush()
        st = models.TrajectoryStep(
            trajectory_id=attempt.trajectory.id, idx=i, action_type="click",
            description=f"agent {i}", actor="agent", before_checkpoint_id=cp.id,
        )
        db_session.add(st)
        steps.append(st)
    db_session.flush()
    versions.adopt_steps(db_session, v, steps)
    db_session.commit()
    return v, steps


def _steps(n=2):
    return [
        {"action_kind": "click", "description": f"corrected {i}", "url_after": f"/x{i}",
         "world_after": {"step": 100 + i}, "reasoning": "because"}
        for i in range(n)
    ]


# --------------------------------------------------------------------------- handoff
def test_a_run_creates_a_candidate_and_never_moves_the_head(db_session, attempt, v1):
    """THE isolation rule for handoff: the annotator's head is theirs to move."""
    root, steps = v1
    versions.set_head(db_session, attempt, root, expected_revision=0)
    job, child = agent_runs.enqueue(
        db_session, attempt=attempt, source_version=root, step=steps[1], correction="check transit first"
    )
    agent_runs.start(db_session, job, owner="worker-1")
    agent_runs.complete(db_session, job, attempt=attempt, child=child, steps=_steps(),
                        trajectory_id=attempt.trajectory.id, guidance="check transit first")
    db_session.commit()

    assert job.status == agent_runs.DONE and job.result_version_id == child.id
    assert child.status == "candidate"
    assert attempt.active_version_id == root.id, "a finished run must not select itself"


def test_the_child_is_bound_to_the_exact_source_it_forked_from(db_session, attempt, v1):
    root, steps = v1
    job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    assert job.source_version_id == root.id
    assert job.source_checkpoint_id == steps[1].before_checkpoint_id == child.fork_checkpoint_id
    assert job.expected_attempt_revision == attempt.revision


def test_the_rejected_step_is_absent_and_the_agent_suffix_follows(db_session, attempt, v1):
    root, steps = v1
    job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1], mode="before")
    agent_runs.complete(db_session, job, attempt=attempt, child=child, steps=_steps(2),
                        trajectory_id=attempt.trajectory.id)
    db_session.commit()

    flat = versions.flatten(db_session, child)
    assert [s.id for s in flat][:1] == [steps[0].id]
    assert steps[1].id not in [s.id for s in flat]
    assert [s.description for s in flat[1:]] == ["corrected 0", "corrected 1"]
    assert [s.actor for s in flat] == ["agent", "agent", "agent"]


def test_the_reviewers_instruction_is_recorded_on_every_step_it_produced(db_session, attempt, v1):
    """Provenance, not decoration: without it, a corrected run reads later as if
    the agent reasoned its way there unaided."""
    root, steps = v1
    reviewer = attempt.annotator_id
    job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.complete(db_session, job, attempt=attempt, child=child, steps=_steps(2),
                        trajectory_id=attempt.trajectory.id,
                        guidance="check transit first", guidance_author_id=reviewer)
    db_session.commit()

    suffix = [s for s in versions.flatten(db_session, child) if s.version_id == child.id]
    assert all(s.guidance_text == "check transit first" for s in suffix)
    assert all(s.guidance_author_id == reviewer and s.intervention_at is not None for s in suffix)


def test_each_produced_step_gets_a_checkpoint_chained_from_the_fork_point(db_session, attempt, v1):
    root, steps = v1
    job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.complete(db_session, job, attempt=attempt, child=child, steps=_steps(2),
                        trajectory_id=attempt.trajectory.id)
    db_session.commit()

    suffix = [s for s in versions.flatten(db_session, child) if s.version_id == child.id]
    assert suffix[0].before_checkpoint_id == job.source_checkpoint_id, "the run starts where the fork does"
    assert suffix[0].after_checkpoint_id == suffix[1].before_checkpoint_id, "checkpoints must chain"
    assert all(s.after_checkpoint_id for s in suffix)


# --------------------------------------------------------------------------- idempotency
def test_a_retried_request_does_not_spawn_a_second_run(db_session, attempt, v1):
    root, steps = v1
    a, ca = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1], idempotency_key="k1")
    b, cb = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1], idempotency_key="k1")
    db_session.commit()
    assert a.id == b.id and ca.id == cb.id
    assert db_session.query(models.AgentRunJob).count() == 1
    assert len(versions.versions_for(db_session, attempt.id)) == 2, "no duplicate candidate"


# --------------------------------------------------------------------------- cap
def test_a_completed_run_burns_exactly_one_call(db_session, attempt, v1):
    root, steps = v1
    for n in range(2):
        job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1],
                                        idempotency_key=f"k{n}")
        agent_runs.complete(db_session, job, attempt=attempt, child=child, steps=_steps(1),
                            trajectory_id=attempt.trajectory.id)
    db_session.commit()
    assert attempt.agent_call_count == 2


def test_an_infrastructure_failure_does_not_burn_a_call(db_session, attempt, v1):
    """The annotator got no model attempt out of it — charging them would push
    them to manual fallback for our outage."""
    root, steps = v1
    job, _ = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.start(db_session, job, owner="w")
    agent_runs.fail(db_session, job, "gym unreachable", infrastructure=True)
    db_session.commit()
    assert job.status == agent_runs.ERROR and job.counts_against_cap is False
    assert attempt.agent_call_count == 0


def test_a_model_failure_does_count(db_session, attempt, v1):
    root, steps = v1
    job, child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.fail(db_session, job, "the agent produced no steps", infrastructure=False)
    db_session.commit()
    assert job.counts_against_cap is True


def test_the_cap_is_enforced_on_the_attempt_not_the_branch(db_session, attempt, v1):
    """Counting per branch would let an annotator reset the cap by forking."""
    root, steps = v1
    attempt.agent_call_count = 3
    db_session.flush()
    branch = versions.fork_before(db_session, parent=root, step=steps[2])
    with pytest.raises(agent_runs.CapExceeded):
        agent_runs.enqueue(db_session, attempt=attempt, source_version=branch, step=steps[0], max_calls=3)


def test_no_cap_configured_means_no_limit(db_session, attempt, v1):
    """The cap ships only after manual fallback passes E2E; until then it must be
    off, not defaulted to some number."""
    root, steps = v1
    attempt.agent_call_count = 99
    db_session.flush()
    job, _ = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    assert job is not None


# --------------------------------------------------------------------------- recovery
def test_a_dead_worker_is_reaped_and_refunded(db_session, attempt, v1):
    root, steps = v1
    job, _ = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.start(db_session, job, owner="w")
    job.heartbeat_at = datetime.utcnow() - timedelta(minutes=30)
    db_session.flush()

    assert agent_runs.reap_stale(db_session, older_than=datetime.utcnow() - timedelta(minutes=10)) == 1
    assert job.status == agent_runs.ERROR and job.counts_against_cap is False


def test_a_live_worker_survives_the_reaper(db_session, attempt, v1):
    root, steps = v1
    job, _ = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1])
    agent_runs.start(db_session, job, owner="w")
    assert agent_runs.reap_stale(db_session, older_than=datetime.utcnow() - timedelta(minutes=10)) == 0
    assert job.status == agent_runs.RUNNING


def test_two_out_of_order_runs_both_land_as_candidates(db_session, attempt, v1):
    """Neither may cross-attribute onto the other, and neither may select itself —
    the annotator picks."""
    root, steps = v1
    slow, slow_child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[1], idempotency_key="a")
    fast, fast_child = agent_runs.enqueue(db_session, attempt=attempt, source_version=root, step=steps[2], idempotency_key="b")
    agent_runs.complete(db_session, fast, attempt=attempt, child=fast_child, steps=_steps(1), trajectory_id=attempt.trajectory.id)
    versions.set_head(db_session, attempt, fast_child, expected_revision=attempt.revision)
    agent_runs.complete(db_session, slow, attempt=attempt, child=slow_child, steps=_steps(1), trajectory_id=attempt.trajectory.id)
    db_session.commit()

    assert attempt.active_version_id == fast_child.id, "the late run must not steal the head"
    assert slow_child.status == "candidate" and fast_child.status == "candidate"
    assert {s.description for s in versions.flatten(db_session, slow_child)} != \
           {s.description for s in versions.flatten(db_session, fast_child)}
