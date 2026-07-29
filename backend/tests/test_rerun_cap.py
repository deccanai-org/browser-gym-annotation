"""The three-run cap: what it costs, what it refunds, and what it cannot be
talked out of.

The cap is only safe to turn on because the manual path works — the annotator who
runs out has to be able to finish by hand (tests/test_e2e_correction_loop.py walks
that path end to end). Everything here follows from that: the number has to be
readable BEFORE a run is spent, an outage must not quietly consume one, and the
refusal has to point at the way forward.

The HTTP half deliberately does NOT run the branch job inline. In production
`jobs.store.submit` starts a thread and returns (app/jobs.py:80), so the POST
answers while the AgentRunJob row is still QUEUED. A fake that completes the job
before the response would model a contract the server does not have, and would
hide the one bypass that matters here: firing several runs before any of them
has had time to be counted.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app import agent_runs, models, versions
from app.api import versions as vapi
from app.config import Settings, settings

CAP = 3
COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "docker-compose.yml"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"CAP-{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"cap-{uuid4().hex[:6]}@test")
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
    """A four-step canonical run, each step with the checkpoint a fork starts from."""
    v = versions.create_root(db_session, attempt_id=attempt.id, base_trajectory_id=attempt.trajectory.id)
    steps = []
    for i in range(4):
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


def _run(db, attempt, version, step, key=""):
    return agent_runs.enqueue(db, attempt=attempt, source_version=version, step=step,
                              idempotency_key=key, max_calls=CAP)


def _land(db, attempt, job, child):
    return agent_runs.complete(db, job, attempt=attempt, child=child,
                               steps=[{"action_kind": "click", "description": "corrected"}],
                               trajectory_id=attempt.trajectory.id)


# --------------------------------------------------------------------------- the number
def test_the_number_an_annotator_reads_is_the_number_the_server_enforces(db_session, attempt, v1):
    """A budget you can only discover by being refused is how someone ends up
    stranded halfway through an attempt."""
    root, steps = v1
    seen = [agent_runs.remaining(db_session, attempt, max_calls=CAP)]
    for n in range(CAP):
        job, child = _run(db_session, attempt, root, steps[1], key=f"k{n}")
        _land(db_session, attempt, job, child)
        seen.append(agent_runs.remaining(db_session, attempt, max_calls=CAP))

    assert seen == [3, 2, 1, 0]
    with pytest.raises(agent_runs.CapExceeded):
        _run(db_session, attempt, root, steps[1], key="one-too-many")


def test_no_cap_configured_means_no_number_to_show(db_session, attempt):
    """Default OFF has to read as absent, not as zero — a UI that renders 0 for
    "unlimited" would tell every annotator they are out of runs."""
    attempt.agent_call_count = 7
    db_session.flush()
    assert agent_runs.remaining(db_session, attempt, max_calls=None) is None
    assert agent_runs.remaining(db_session, attempt, max_calls=0) is None
    assert settings.agent_run_cap == 0, "the code default stays off; dev sets 3 in docker-compose"


def test_the_dev_stack_turns_the_cap_on_by_a_name_the_settings_actually_read(monkeypatch):
    """The compose file is the only place the cap is switched on. A setting whose
    env name does not match is not a configuration mistake anyone notices — the
    stack simply runs uncapped and every panel says there is no cap."""
    assert COMPOSE.exists(), COMPOSE
    assert f"AGENT_RUN_CAP: ${{AGENT_RUN_CAP:-{CAP}}}" in COMPOSE.read_text(), \
        "dev sets the cap in docker-compose, and it has to be this exact variable"

    monkeypatch.setenv("AGENT_RUN_CAP", str(CAP))
    assert Settings(_env_file=None).agent_run_cap == CAP


def test_the_refusal_names_the_manual_path_rather_than_only_saying_no(db_session, attempt, v1):
    root, steps = v1
    attempt.agent_call_count = CAP
    db_session.flush()
    with pytest.raises(agent_runs.CapExceeded) as exc:
        _run(db_session, attempt, root, steps[1])
    said = str(exc.value)
    assert "manually" in said
    assert "reject the step" in said and "commit your own actions" in said, \
        "the annotator is at the wall — the way forward has to be in the sentence that stops them"


# --------------------------------------------------------------------------- in flight
def test_a_run_still_in_flight_has_already_been_spent(db_session, attempt, v1):
    """The counter only moves when a job ends, and the request returns while the
    job is queued. Counting only landed runs let three requests in a row each read
    zero-spent and all pass a cap of three."""
    root, steps = v1
    for n in range(CAP):
        job, _ = _run(db_session, attempt, root, steps[1], key=f"k{n}")
        assert job.status == agent_runs.QUEUED

    assert attempt.agent_call_count == 0, "nothing has landed yet"
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 0
    with pytest.raises(agent_runs.CapExceeded):
        _run(db_session, attempt, root, steps[2], key="fourth")


def test_a_landing_run_does_not_pay_twice(db_session, attempt, v1):
    """It is reserved while queued and charged when it lands; if the two stacked,
    a cap of three would buy one run and a half."""
    root, steps = v1
    job, child = _run(db_session, attempt, root, steps[1], key="k0")
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2
    _land(db_session, attempt, job, child)
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2
    assert attempt.agent_call_count == 1


# --------------------------------------------------------------------------- refunds
def test_an_infrastructure_failure_gives_the_run_back_visibly(db_session, attempt, v1):
    """An annotator who believes our outage burned a run stops trusting the number,
    and a number nobody trusts is worse than no number."""
    root, steps = v1
    attempt.agent_call_count = CAP - 1
    db_session.flush()
    job, _ = _run(db_session, attempt, root, steps[1], key="k0")
    agent_runs.start(db_session, job, owner="w")
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 0

    agent_runs.fail(db_session, job, "gym unreachable", infrastructure=True)

    assert job.counts_against_cap is False, "the listing has to SHOW the refund, not just imply it"
    assert attempt.agent_call_count == CAP - 1
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 1
    assert _run(db_session, attempt, root, steps[2], key="k1")[0] is not None


def test_a_model_failure_moves_the_counter_and_not_only_the_flag(db_session, attempt, v1):
    """The annotator did get their model attempt. Marking the job as counting while
    leaving the counter alone would hand back a free run every time the model
    itself failed, and the cap would not hold."""
    root, steps = v1
    job, _ = _run(db_session, attempt, root, steps[1], key="k0")
    agent_runs.fail(db_session, job, "the agent produced no steps", infrastructure=False)

    assert job.counts_against_cap is True
    assert attempt.agent_call_count == 1, "a counted failure that never charges is a free run"
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2


def test_a_job_reported_terminal_twice_is_charged_once(db_session, attempt, v1):
    """The reaper and a late worker can both report the same job. Charging per
    report spends runs the annotator never received."""
    root, steps = v1
    job, child = _run(db_session, attempt, root, steps[1], key="k0")
    agent_runs.fail(db_session, job, "the agent produced no steps", infrastructure=False)
    agent_runs.fail(db_session, job, "the agent produced no steps (retried report)", infrastructure=False)
    _land(db_session, attempt, job, child)

    assert attempt.agent_call_count == 1
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2


def test_a_queued_run_no_worker_ever_took_is_refunded(db_session, attempt, v1):
    """It holds a reservation and has no heartbeat to go stale, so without this it
    would keep one of the annotator's three runs for good."""
    root, steps = v1
    job, _ = _run(db_session, attempt, root, steps[1], key="k0")
    db_session.query(models.AgentRunJob).filter(models.AgentRunJob.id == job.id).update(
        {"created_at": datetime.utcnow() - timedelta(hours=2)}
    )
    db_session.flush()
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2

    assert agent_runs.reap_stale(db_session, older_than=datetime.utcnow() - timedelta(minutes=10)) == 1

    db_session.refresh(job)
    assert job.status == agent_runs.ERROR and job.counts_against_cap is False
    assert "picked this run up" in job.error
    assert agent_runs.remaining(db_session, attempt, max_calls=CAP) == 3


# --------------------------------------------------------------------------- over HTTP
@pytest.fixture()
def async_branch(monkeypatch, _session_factory):
    """The real handoff contract: the route returns while the job is still QUEUED.

    `jobs.store.submit` runs `fn` on a thread and returns a handle without waiting
    (app/jobs.py:80). This fake keeps the function UNRUN so the row stays queued,
    which is exactly the window the cap has to survive. It also returns the queued
    jobs so a test can land them by hand.
    """
    from app.api import gym as gym_api

    monkeypatch.setattr(vapi, "SessionLocal", _session_factory)

    class Worker:
        base_url = "http://fake-branch-worker"

        def resume_run(self, *a, **k):
            return {"trajectory": {"steps": [{"action_kind": "click", "description": "corrected"}]}}

        def world(self):
            return {"step": 9}

    @contextlib.contextmanager
    def workspace(_attempt_id):
        yield Worker()

    monkeypatch.setattr(gym_api, "_agent_workspace", workspace)

    class Handle:
        id = "bg-1"
        status = "queued"

    pending: list = []

    def submit(_name, fn, *args):
        pending.append((fn, args))
        return Handle()

    monkeypatch.setattr(vapi.jobs.store, "submit", submit)
    return pending


@pytest.fixture()
def gym_task(db_session):
    """A gym task whose canonical run an attempt can baseline from."""
    task = models.Task(external_id=f"M77_cap/{uuid4().hex[:6]}", title="cap", prompt="do it", source="gym")
    sysann = models.Annotator(email=f"sys-{uuid4().hex[:6]}@system.local")
    db_session.add_all([task, sysann])
    db_session.flush()
    bench = models.ReviewSession(task_id=task.id, annotator_id=sysann.id, source="gym", status="benchmark_run")
    db_session.add(bench)
    db_session.flush()
    traj = models.Trajectory(session_id=bench.id, agent="gpt-5.5", source="gym", raw={"steps": []})
    db_session.add(traj)
    db_session.flush()
    prev = None
    for i in range(4):
        cp = models.EnvironmentCheckpoint(attempt_id=bench.id, world={"step": i}, step_clock=i)
        db_session.add(cp)
        db_session.flush()
        db_session.add(models.TrajectoryStep(
            trajectory_id=traj.id, idx=i, action_type="click", description=f"agent step {i}",
            actor="agent", before_checkpoint_id=cp.id, after_checkpoint_id=None,
        ))
        prev = cp
    assert prev is not None
    db_session.commit()
    return task


@pytest.fixture()
def capped(monkeypatch):
    monkeypatch.setattr(settings, "agent_run_cap", CAP)


def _open(client, task, fresh=False):
    return client.post(f"/api/tasks/{task.external_id}/sessions", json={"fresh": fresh}).json()["sessionId"]


def _baseline(client, sid):
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    return v1, steps


def _agent_run(client, sid, version_id, step_id, key=""):
    return client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": version_id, "stepId": step_id, "mode": "before", "idempotencyKey": key,
    })


def test_the_running_app_refuses_a_fourth_run_and_says_how_to_finish(client, gym_task, capped, async_branch):
    """Through the mounted route, not the service — a policy that only holds when
    called directly is a policy the app does not have."""
    sid = _open(client, gym_task)
    v1, steps = _baseline(client, sid)

    for n in range(CAP):
        assert _agent_run(client, sid, v1["id"], steps[1]["stepId"], key=f"k{n}").status_code == 200

    refused = _agent_run(client, sid, v1["id"], steps[2]["stepId"], key="fourth")
    assert refused.status_code == 429
    assert "manually" in refused.json()["detail"] and "commit your own actions" in refused.json()["detail"]


def test_the_annotator_can_read_the_budget_before_spending_the_next_run(
    client, gym_task, capped, async_branch, db_session
):
    """The read path the panel actually uses. The queued job has to be visible as
    counting, or the client cannot derive the same number the server enforces."""
    sid = _open(client, gym_task)
    v1, steps = _baseline(client, sid)

    fresh = client.get(f"/api/sessions/{sid}/runs").json()
    assert fresh["cap"] == CAP and fresh["agentCallCount"] == 0 and fresh["runs"] == []

    _agent_run(client, sid, v1["id"], steps[1]["stepId"], key="k0")

    listed = client.get(f"/api/sessions/{sid}/runs").json()
    assert listed["cap"] == CAP
    assert [(r["status"], r["countsAgainstCap"]) for r in listed["runs"]] == [("queued", True)]

    # The whole round trip: what the panel computes off this payload
    # (frontend/src/features/versions/VersionGraph.tsx, `runBudget`) has to equal
    # what the server would actually allow. Two numbers derived independently
    # from the same state is how an annotator ends up refused against a budget
    # they were just shown.
    reserved = sum(1 for r in listed["runs"] if r["countsAgainstCap"] and r["status"] in ("queued", "running"))
    panel_shows = listed["cap"] - listed["agentCallCount"] - reserved
    attempt = db_session.get(models.ReviewSession, UUID(sid))
    assert panel_shows == agent_runs.remaining(db_session, attempt, max_calls=CAP) == 2


def test_an_outage_shows_up_in_the_listing_as_a_run_that_did_not_count(
    client, gym_task, capped, async_branch, db_session
):
    sid = _open(client, gym_task)
    v1, steps = _baseline(client, sid)
    _agent_run(client, sid, v1["id"], steps[1]["stepId"], key="k0")

    job = db_session.query(models.AgentRunJob).one()
    agent_runs.fail(db_session, job, "gym unreachable or resume failed", infrastructure=True)
    db_session.commit()

    listed = client.get(f"/api/sessions/{sid}/runs").json()
    assert [(r["status"], r["countsAgainstCap"]) for r in listed["runs"]] == [("error", False)]
    assert listed["agentCallCount"] == 0
    assert _agent_run(client, sid, v1["id"], steps[2]["stepId"], key="k1").status_code == 200, \
        "the refunded run is really spendable again"


# --------------------------------------------------------------------------- bypasses
def test_forking_a_sibling_branch_does_not_reset_the_cap(client, gym_task, capped, async_branch):
    """The counter lives on the ATTEMPT. Per branch, an annotator could mint a
    fresh budget with one fork."""
    sid = _open(client, gym_task)
    v1, steps = _baseline(client, sid)
    for n in range(CAP):
        assert _agent_run(client, sid, v1["id"], steps[1]["stepId"], key=f"k{n}").status_code == 200

    sibling = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before"})
    assert sibling.status_code == 200

    assert _agent_run(client, sid, sibling.json()["id"], steps[1]["stepId"], key="on-sibling").status_code == 429


def test_retrying_the_same_idempotency_key_does_not_buy_another_run(client, gym_task, capped, async_branch, db_session):
    """A retry has to be free — it is the same run — and it must not be turned
    into a way of getting a fourth one."""
    sid = _open(client, gym_task)
    v1, steps = _baseline(client, sid)
    first = [_agent_run(client, sid, v1["id"], steps[1]["stepId"], key=f"k{n}") for n in range(CAP)]
    assert [r.status_code for r in first] == [200] * CAP

    again = _agent_run(client, sid, v1["id"], steps[1]["stepId"], key="k0")

    assert again.status_code == 200, "the retry of an accepted run is not a new spend"
    assert again.json()["versionId"] == first[0].json()["versionId"]
    assert db_session.query(models.AgentRunJob).count() == CAP, "no fourth job exists"


def test_a_second_attempt_on_the_same_task_starts_a_fresh_budget(client, gym_task, capped, async_branch, db_session):
    """KNOWN GAP, asserted rather than assumed so it cannot be discovered as a
    surprise. The cap is scoped to the attempt (§3.9), and `fresh=true` on
    POST /api/tasks/{id}/sessions opens a second attempt on the same task for the
    same annotator (app/api/sessions.py:588) whose counter starts at zero.

    Widening the scope is not a fix that can be made here: /runs is per attempt,
    so a task-wide cap would be invisible to the panel — the annotator would be
    refused against a number they were never shown, which is the stranding this
    whole feature is meant to avoid.
    """
    first = _open(client, gym_task)
    v1, steps = _baseline(client, first)
    for n in range(CAP):
        assert _agent_run(client, first, v1["id"], steps[1]["stepId"], key=f"k{n}").status_code == 200
    assert _agent_run(client, first, v1["id"], steps[2]["stepId"], key="x").status_code == 429

    second = _open(client, gym_task, fresh=True)
    assert second != first
    v1b, steps_b = _baseline(client, second)
    assert _agent_run(client, second, v1b["id"], steps_b[1]["stepId"], key="k0").status_code == 200

    budget = client.get(f"/api/sessions/{second}/runs").json()
    assert budget["cap"] == CAP and budget["agentCallCount"] == 0
    assert client.get(f"/api/sessions/{first}/runs").json()["runs"], "the first attempt keeps its spent runs"
