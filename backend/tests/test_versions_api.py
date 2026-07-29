"""Version-graph API: baseline, fork, select, verdict — over HTTP, with the
ownership and concurrency gates that make it safe for two annotators at once."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app import models


@pytest.fixture()
def gym_task(db_session):
    """A gym task with a canonical recorded run — what a breaker looks like once
    a run-review has persisted it."""
    task = models.Task(external_id=f"M99_demo/{uuid4().hex[:6]}", title="demo", prompt="do the thing", source="gym")
    sysann = models.Annotator(email=f"sys-{uuid4().hex[:6]}@system.local")
    db_session.add_all([task, sysann])
    db_session.flush()
    bench = models.ReviewSession(task_id=task.id, annotator_id=sysann.id, source="gym", status="benchmark_run")
    db_session.add(bench)
    db_session.flush()
    traj = models.Trajectory(session_id=bench.id, agent="gpt-5.5", source="gym", raw={"steps": []})
    db_session.add(traj)
    db_session.flush()
    for i in range(4):
        db_session.add(models.TrajectoryStep(
            trajectory_id=traj.id, idx=i, action_type="click",
            description=f"agent step {i}", actor="agent", url_after=f"/p{i}",
        ))
    db_session.commit()
    return task


def _open(client, external_id):
    r = client.post(f"/api/tasks/{external_id}/sessions", json={})
    assert r.status_code == 200, r.text
    return r.json()["sessionId"]


def _baseline(client, sid):
    r = client.post(f"/api/sessions/{sid}/versions/baseline")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- baseline
def test_baseline_materializes_v1_from_the_canonical_run(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    assert v1["versionNo"] == 1 and v1["kind"] == "agent_run" and v1["stepCount"] == 4
    assert v1["status"] == "candidate", "a recorded run is a candidate until QC says otherwise"

    again = _baseline(client, sid)
    assert again["id"] == v1["id"], "baselining twice must not mint a second root"


def test_baseline_without_a_recorded_run_is_a_clean_409(client, db_session):
    task = models.Task(external_id=f"empty-{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    db_session.add(task)
    db_session.commit()
    sid = _open(client, task.external_id)
    r = client.post(f"/api/sessions/{sid}/versions/baseline")
    assert r.status_code == 409 and "no recorded run" in r.json()["detail"]


def test_two_annotators_get_independent_graphs_over_the_same_breaker(client_for, gym_task):
    """The canonical run is shared; the annotation of it is not. Sharing step rows
    would let one annotator's fork rewrite the other's."""
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa, sb = _open(a, gym_task.external_id), _open(b, gym_task.external_id)
    va, vb = _baseline(a, sa), _baseline(b, sb)
    assert sa != sb and va["id"] != vb["id"]

    steps_a = a.get(f"/api/sessions/{sa}/versions/{va['id']}/steps").json()["steps"]
    steps_b = b.get(f"/api/sessions/{sb}/versions/{vb['id']}/steps").json()["steps"]
    assert [s["description"] for s in steps_a] == [s["description"] for s in steps_b]
    assert {s["stepId"] for s in steps_a}.isdisjoint({s["stepId"] for s in steps_b})


# --------------------------------------------------------------------------- fork
def test_fork_before_drops_the_rejected_step_over_http(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]

    r = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before",
    })
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["versionNo"] == 2 and v2["parentId"] == v1["id"] and v2["stepCount"] == 2

    child = client.get(f"/api/sessions/{sid}/versions/{v2['id']}/steps").json()["steps"]
    assert steps[2]["stepId"] not in [s["stepId"] for s in child]
    assert all(s["inherited"] for s in child), "the child has no suffix yet — all inherited"


def test_continue_after_keeps_the_step(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    r = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "after",
    })
    assert r.json()["stepCount"] == 3, "continuing keeps the step it resumes from"


def test_forking_on_another_annotators_step_is_not_found(client_for, gym_task):
    """A step UUID from someone else's attempt must never be a usable handle."""
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa, sb = _open(a, gym_task.external_id), _open(b, gym_task.external_id)
    va, vb = _baseline(a, sa), _baseline(b, sb)
    theirs = b.get(f"/api/sessions/{sb}/versions/{vb['id']}/steps").json()["steps"][1]["stepId"]

    r = a.post(f"/api/sessions/{sa}/versions/fork", json={
        "parentVersionId": va["id"], "stepId": theirs, "mode": "before",
    })
    assert r.status_code == 404


def test_a_version_from_another_attempt_cannot_be_selected(client_for, gym_task):
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa, sb = _open(a, gym_task.external_id), _open(b, gym_task.external_id)
    _baseline(a, sa)
    vb = _baseline(b, sb)
    r = a.post(f"/api/sessions/{sa}/versions/select", json={"versionId": vb["id"], "expectedRevision": 0})
    assert r.status_code == 404


# --------------------------------------------------------------------------- head
def test_selecting_a_version_advances_the_head_under_cas(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    listing = client.get(f"/api/sessions/{sid}/versions").json()
    assert listing["headVersionId"] is None and listing["revision"] == 0

    r = client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v1["id"], "expectedRevision": 0})
    assert r.status_code == 200 and r.json()["revision"] == 1

    stale = client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v1["id"], "expectedRevision": 0})
    assert stale.status_code == 409, "a stale client must reload, not clobber"

    after = client.get(f"/api/sessions/{sid}/versions").json()
    assert after["headVersionId"] == v1["id"]
    assert [v["isHead"] for v in after["versions"]] == [True]


def test_forking_does_not_move_the_head(client, gym_task):
    """A finished branch is a CANDIDATE. Auto-advancing is exactly how an
    out-of-order agent completion would overwrite a newer decision."""
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v1["id"], "expectedRevision": 0})
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[1]["stepId"], "mode": "before",
    })
    assert client.get(f"/api/sessions/{sid}/versions").json()["headVersionId"] == v1["id"]


# --------------------------------------------------------------------------- status
def test_qc_status_transitions_are_guarded(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    r = client.post(f"/api/sessions/{sid}/versions/{v1['id']}/status", json={"status": "approved", "expectedRevision": 0})
    assert r.status_code == 200 and r.json()["status"] == "approved"

    assert client.post(f"/api/sessions/{sid}/versions/{v1['id']}/status",
                       json={"status": "rejected", "expectedRevision": 0}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/versions/{v1['id']}/status",
                       json={"status": "shipped", "expectedRevision": 1}).status_code == 422


# --------------------------------------------------------------------------- verdicts
def test_verdicts_are_keyed_by_step_and_survive_a_fork(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    for s in steps[:2]:
        assert client.post(f"/api/sessions/{sid}/steps/verdict",
                           json={"stepId": s["stepId"], "verdict": "verified"}).status_code == 200
    client.post(f"/api/sessions/{sid}/steps/verdict",
                json={"stepId": steps[2]["stepId"], "verdict": "rejected", "note": "wrong product"})

    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before",
    }).json()

    child = client.get(f"/api/sessions/{sid}/versions/{v2['id']}/steps").json()["steps"]
    assert [s["verdict"] for s in child] == ["verified", "verified"], (
        "the annotator's completed review must carry over to the branch"
    )
    assert client.get(f"/api/sessions/{sid}/versions").json()["verdicts"][steps[2]["stepId"]]["note"] == "wrong product"


def test_an_unknown_verdict_value_is_rejected(client, gym_task):
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    step = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"][0]
    assert client.post(f"/api/sessions/{sid}/steps/verdict",
                       json={"stepId": step["stepId"], "verdict": "maybe"}).status_code == 422


def test_the_whole_surface_requires_authentication(anon_client, gym_task):
    for path, method in [
        ("/api/sessions/%s/versions" % uuid4(), "get"),
        ("/api/sessions/%s/versions/baseline" % uuid4(), "post"),
        ("/api/sessions/%s/versions/fork" % uuid4(), "post"),
        ("/api/sessions/%s/steps/verdict" % uuid4(), "post"),
    ]:
        r = anon_client.post(path, json={}) if method == "post" else anon_client.get(path)
        assert r.status_code == 401, path


# --------------------------------------------------------------------------- v1/v2 divergence
def test_the_legacy_submit_refuses_to_ship_a_golden_that_contradicts_the_version_head(client, gym_task, db_session):
    """Both correction systems are reachable from the same screen, and the legacy
    one builds its golden from the recorded run — it knows nothing about versions.
    Measured before this guard: an annotator forked before step 2 (rejecting it),
    pressed the old Submit, and the exported sample carried all three steps
    including the rejected one, with no binding and no sign anything was wrong.
    Silently shipping a golden that contradicts the annotator's own decision is
    worse than any error."""
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before",
    }).json()
    client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v2["id"], "expectedRevision": 0})

    s = db_session.get(models.ReviewSession, UUID(sid))
    s.status = "benchmark_run"
    suite = models.VerifierSuite(session_id=s.id, version=1)
    db_session.add(suite)
    db_session.flush()
    db_session.add(models.BenchmarkRun(suite_id=suite.id, reward=1, results={}))
    db_session.commit()

    r = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "kind": "golden"})
    assert r.status_code == 409
    assert "finalize" in r.json()["detail"], "point at the path that DOES honour the work"
    assert db_session.query(models.Submission).filter(models.Submission.session_id == s.id).count() == 0


def test_the_legacy_submit_still_works_on_an_uncorrected_attempt(client, gym_task, db_session):
    """The guard must not block the ordinary case: a run that needed no correction
    has only v1, which IS the recorded run, so the legacy golden agrees with it."""
    sid = _open(client, gym_task.external_id)
    v1 = _baseline(client, sid)
    client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v1["id"], "expectedRevision": 0})

    s = db_session.get(models.ReviewSession, UUID(sid))
    s.status = "benchmark_run"
    s.reviewed_through = 99
    suite = models.VerifierSuite(session_id=s.id, version=1)
    db_session.add(suite)
    db_session.flush()
    db_session.add(models.BenchmarkRun(suite_id=suite.id, reward=1, results={}))
    db_session.commit()

    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "kind": "golden"}).status_code == 200
