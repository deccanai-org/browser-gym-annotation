"""Disposition over HTTP — propose, adjudicate, rework, and count.

The invariant under test throughout: a failed attempt must end up with an answer
to "was it the model or was it us?" that somebody other than the person who
failed has agreed to.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app import models
from app.api.disposition import router as disposition_router
from app.main import app

def test_the_router_is_registered_in_the_app_itself():
    """The suite passed 25 tests while every one of these routes 404'd in the real
    application, because the router was only mounted inside this file. A feature
    the app does not serve is not a feature — assert the wiring, not just the
    handlers."""
    assert any(getattr(r, "original_router", None) is disposition_router for r in app.routes), (
        "app/main.py must include the disposition router"
    )


@pytest.fixture()
def breaker(db_session):
    """A gym breaker whose canonical run left evidence behind — the shape every
    one of the 85 report tasks has."""
    task = models.Task(
        external_id=f"M99_broken/{uuid4().hex[:6]}", title="demo",
        prompt="do the thing", source="gym",
    )
    sysann = models.Annotator(email=f"sys-{uuid4().hex[:6]}@system.local")
    db_session.add_all([task, sysann])
    db_session.flush()
    # A SYSTEM run carries an agent; a human attempt never does. The aggregate
    # leans on exactly that to keep canonical-run rows out of its denominator.
    canonical = models.ReviewSession(
        task_id=task.id, annotator_id=sysann.id, source="gym",
        agent="gpt-5.5", status="benchmark_run",
    )
    db_session.add(canonical)
    db_session.flush()
    art = models.Artifact(kind="screenshot", uri="file:///runs/step3.png", sha256="a" * 64, bytes=17)
    cp = models.EnvironmentCheckpoint(
        attempt_id=canonical.id, world={"shop": {"cart": []}},
        environment_image_digest="sha256:envA",
    )
    db_session.add_all([art, cp])
    db_session.commit()
    return SimpleNamespace(
        task=task, external_id=task.external_id,
        artifact_id=str(art.id), checkpoint_id=str(cp.id),
    )


def _open(client, external_id: str) -> str:
    r = client.post(f"/api/tasks/{external_id}/sessions", json={})
    assert r.status_code == 200, r.text
    return r.json()["sessionId"]


def _evidence(breaker) -> list[dict]:
    return [
        {"artifactId": breaker.artifact_id, "note": "the 500 on add-to-cart"},
        {"checkpointId": breaker.checkpoint_id},
    ]


def _stamp_environment(db, sid: str, digest: str = "sha256:envA") -> None:
    """Give the attempt a real environment to be found in. The server derives the
    digest from the attempt itself — a claimant does not get to name the
    environment they are blaming — so a test that cares about it has to create
    one, exactly as a real run would."""
    db.add(models.EnvironmentCheckpoint(attempt_id=UUID(sid), world={}, environment_image_digest=digest))
    db.commit()


def _propose(client, sid: str, **over):
    body = {
        "disposition": "environment_broken",
        "note": "add-to-cart 500s on every retry",
        "evidence": [],
        "expectedRevision": 0,
    }
    body.update(over)
    return client.post(f"/api/sessions/{sid}/disposition", json=body)


def _decide(client, sid: str, **over):
    body = {"decision": "accept", "note": "", "expectedRevision": 1}
    body.update(over)
    return client.post(f"/api/sessions/{sid}/disposition/decision", json=body)


# --------------------------------------------------------------------------- propose
def test_an_annotator_proposes_a_disposition_with_evidence_and_it_reads_back(client, breaker):
    sid = _open(client, breaker.external_id)
    r = _propose(client, sid, evidence=_evidence(breaker))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["disposition"] == "environment_broken"
    assert out["standing"] == "proposed", "the annotator's own claim is not yet an answer"
    assert len(out["evidence"]) == 2 and out["revision"] == 1

    reloaded = client.get(f"/api/sessions/{sid}/disposition").json()
    assert reloaded["disposition"] == "environment_broken" and reloaded["round"] == 1
    assert reloaded["taxonomy"] == [
        "model_failure", "task_unsolvable", "environment_broken",
        "seed_invalid", "instruction_ambiguous", "verifier_invalid",
    ]


def test_a_value_outside_the_taxonomy_is_refused(client, breaker):
    """Free text here is how the column ended up unusable the first time."""
    sid = _open(client, breaker.external_id)
    assert _propose(client, sid, disposition="flaky", evidence=_evidence(breaker)).status_code == 422


def test_blaming_the_harness_without_evidence_is_refused(client, client_for, breaker):
    """An unevidenced "environment_broken" is a shrug, and a report full of shrugs
    sends someone hunting a bug that was never there. Blaming the model needs no
    citation — that is the claim the benchmark already makes."""
    sid = _open(client, breaker.external_id)
    assert _propose(client, sid, evidence=[]).status_code == 422

    other = client_for("nav@deccan.ai")
    sid2 = _open(other, breaker.external_id)
    r = _propose(other, sid2, disposition="model_failure", note="clicked the wrong product",
                 evidence=[])
    assert r.status_code == 200, r.text


def test_evidence_that_cites_a_row_which_does_not_exist_is_refused(client, breaker):
    """A dangling citation reads as proof in the report and evaporates when opened."""
    sid = _open(client, breaker.external_id)
    r = _propose(client, sid, evidence=[{"artifactId": str(uuid4())}])
    assert r.status_code == 422 and "no artifact" in r.json()["detail"]
    assert _propose(client, sid, evidence=[{"note": "trust me"}]).status_code == 422


def test_a_disposition_records_which_environment_and_task_revision_it_was_made_in(client, breaker, db_session):
    """A fix cannot be verified against an environment nobody named."""
    sid = _open(client, breaker.external_id)
    db_session.add(models.EnvironmentCheckpoint(
        attempt_id=UUID(sid), world={}, environment_image_digest="sha256:envA",
    ))
    db_session.commit()
    out = _propose(client, sid, evidence=_evidence(breaker)).json()
    assert out["environmentImageDigest"] == "sha256:envA" and out["taskRevision"] == 1
    assert out["history"][0]["environmentImageDigest"] == "sha256:envA"
    assert out["history"][0]["taskRevision"] == 1


def test_the_claimant_cannot_name_the_environment_they_are_blaming(client, breaker, db_session):
    """The one person with a motive to blame the environment is the person whose
    attempt just failed. If they supply the digest, a fix gets verified against an
    image the attempt never ran in — and the claim still looks fully evidenced."""
    sid = _open(client, breaker.external_id)
    db_session.add(models.EnvironmentCheckpoint(
        attempt_id=UUID(sid), world={}, environment_image_digest="sha256:real",
    ))
    db_session.commit()
    out = _propose(client, sid, disposition="environment_broken", evidence=_evidence(breaker),
                   environmentImageDigest="sha256:whatever-i-say").json()
    assert out["environmentImageDigest"] == "sha256:real"


def test_the_environment_digest_falls_back_to_the_attempts_own_checkpoint(client, breaker, db_session):
    sid = _open(client, breaker.external_id)
    db_session.add(models.EnvironmentCheckpoint(
        attempt_id=UUID(sid), world={}, environment_image_digest="sha256:envB",
    ))
    db_session.commit()
    out = _propose(client, sid, evidence=_evidence(breaker)).json()
    assert out["environmentImageDigest"] == "sha256:envB"


def test_an_annotator_cannot_read_or_disposition_another_annotators_attempt(client_for, breaker):
    """404, not 403 — a 403 would confirm the attempt exists."""
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa = _open(a, breaker.external_id)
    assert b.get(f"/api/sessions/{sa}/disposition").status_code == 404
    assert _propose(b, sa, evidence=_evidence(breaker)).status_code == 404


def test_a_stale_expected_revision_loses_the_compare_and_swap(client, breaker):
    """Two people on the same stale screen must not both get to write the answer."""
    sid = _open(client, breaker.external_id)
    assert _propose(client, sid, evidence=_evidence(breaker)).status_code == 200
    r = _propose(client, sid, evidence=_evidence(breaker), note="second look", expectedRevision=0)
    assert r.status_code == 409


# --------------------------------------------------------------------------- adjudicate
def test_a_reviewer_accepting_turns_a_proposal_into_an_adjudicated_answer(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    r = _decide(reviewer_client, sid, note="reproduced on a clean workspace")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["standing"] == "adjudicated" and out["disposition"] == "environment_broken"
    assert out["by"] == "reviewer@deccan.ai"
    assert out["history"][-1]["event"] == "accepted"


def test_a_reviewer_rejecting_must_name_the_disposition_that_actually_holds(client, reviewer_client, breaker):
    """A bare "no" leaves the report exactly where it started — still unable to say
    whether the model failed."""
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    why = "the 500 came from the agent's own malformed payload"
    assert _decide(reviewer_client, sid, decision="reject", note=why).status_code == 422
    assert _decide(reviewer_client, sid, decision="reject", note=why,
                   disposition="environment_broken").status_code == 422

    r = _decide(reviewer_client, sid, decision="reject", note=why, disposition="model_failure")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["disposition"] == "model_failure" and out["standing"] == "adjudicated"
    assert out["note"] == why, "the standing note must describe the value that stands"
    assert out["history"][-1]["replaced"] == "environment_broken"


def test_accepting_with_a_different_value_is_refused_as_a_disguised_reject(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    r = _decide(reviewer_client, sid, disposition="seed_invalid", note="actually the seed")
    assert r.status_code == 422


def test_a_reviewer_cannot_adjudicate_their_own_attempt(reviewer_client, breaker):
    """Self-adjudication makes `standing` a lie: the column split cannot tell it
    apart from an independent ruling."""
    sid = _open(reviewer_client, breaker.external_id)
    _propose(reviewer_client, sid, evidence=_evidence(breaker))
    assert _decide(reviewer_client, sid, note="looks right to me").status_code == 403


def test_adjudication_is_closed_to_a_plain_annotator(client, client_for, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    assert _decide(client_for("nav@deccan.ai"), sid, note="fine").status_code == 403


def test_adjudicating_an_attempt_nobody_proposed_for_is_a_409(client, reviewer_client, breaker):
    """Ruling on an empty claim would manufacture a verdict out of nothing."""
    sid = _open(client, breaker.external_id)
    assert _decide(reviewer_client, sid, note="ok", expectedRevision=0).status_code == 409


def test_an_annotator_cannot_overwrite_a_reviewers_ruling(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    assert _decide(reviewer_client, sid, note="reproduced").status_code == 200
    r = _propose(client, sid, evidence=_evidence(breaker), expectedRevision=2)
    assert r.status_code == 409, "adjudication is decorative if the proposer gets the last word"


# --------------------------------------------------------------------------- rework
def test_requested_rework_reopens_the_claim_and_the_next_round_marks_it_done(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    r = _decide(reviewer_client, sid, decision="request_rework",
                note="attach the DOM dump — a screenshot of a 500 proves nothing")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["reworkStatus"] == "requested"
    assert out["standing"] == "proposed", "rework is not a ruling — nothing was adjudicated"

    again = _propose(client, sid, evidence=_evidence(breaker), note="dom dump attached",
                     expectedRevision=out["revision"]).json()
    assert again["reworkStatus"] == "done" and again["round"] == 2
    assert [h["event"] for h in again["history"]] == ["proposed", "rework_requested", "proposed"]


def test_requesting_rework_without_saying_what_to_redo_is_refused(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    assert _decide(reviewer_client, sid, decision="request_rework", note="").status_code == 422


# --------------------------------------------------------------------------- aggregate
def test_the_summary_splits_each_disposition_into_adjudicated_and_proposed(client_for, reviewer_client, breaker, db_session):
    """The blocker itself: forty attempts calling themselves environment_broken mean
    nothing until someone who did not run them agrees."""
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa, sb = _open(a, breaker.external_id), _open(b, breaker.external_id)
    _stamp_environment(db_session, sa)
    _propose(a, sa, evidence=_evidence(breaker))
    _propose(b, sb, disposition="model_failure", note="clicked the wrong product", evidence=[])
    assert _decide(reviewer_client, sa, note="reproduced").status_code == 200

    out = reviewer_client.get("/api/dispositions/summary").json()
    assert out["totals"]["attempts"] == 2, "the canonical system run is not an attempt"
    assert out["totals"]["adjudicated"] == 1 and out["totals"]["proposed"] == 1
    assert out["totals"]["undisposed"] == 0
    assert out["byDisposition"]["environment_broken"] == {"proposed": 0, "adjudicated": 1, "total": 1}
    assert out["byDisposition"]["model_failure"] == {"proposed": 1, "adjudicated": 0, "total": 1}
    assert set(out["byDisposition"]) == set(out["taxonomy"]), "zero-filled, so the table is stable"
    assert out["byEnvironment"]["sha256:envA"]["dispositions"]["environment_broken"] == 1


def test_the_summary_can_be_scoped_to_a_set_of_tasks(client, reviewer_client, breaker, db_session):
    other = models.Task(external_id=f"M98_other/{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    db_session.add(other)
    db_session.commit()
    s1 = _open(client, breaker.external_id)
    _propose(client, s1, evidence=_evidence(breaker))
    s2 = _open(client, other.external_id)
    _propose(client, s2, disposition="model_failure", note="wrong product", evidence=[])

    scoped = reviewer_client.get(
        "/api/dispositions/summary", params={"task": [breaker.external_id]}
    ).json()
    assert scoped["tasks"] == [breaker.external_id]
    assert scoped["totals"]["attempts"] == 1
    assert scoped["byDisposition"]["model_failure"]["total"] == 0
    assert scoped["byTask"][0]["byDisposition"]["environment_broken"]["proposed"] == 1


def test_an_attempt_with_no_disposition_is_counted_as_undisposed_not_dropped(client, reviewer_client, breaker):
    """An attempt nobody dispositioned is the report's real backlog — it must not
    vanish from the denominator."""
    _open(client, breaker.external_id)
    out = reviewer_client.get("/api/dispositions/summary").json()
    assert out["totals"] == {"attempts": 1, "undisposed": 1, "proposed": 0,
                             "adjudicated": 0, "reworkRequested": 0}


def test_the_aggregate_surfaces_are_reviewer_only(client, breaker):
    """They read every annotator's failure record, like the dataset export."""
    assert client.get("/api/dispositions/summary").status_code == 403
    assert client.get("/api/dispositions/queue").status_code == 403


def test_the_reviewer_queue_lists_proposals_still_waiting_on_a_ruling(client, reviewer_client, breaker, db_session):
    sid = _open(client, breaker.external_id)
    _stamp_environment(db_session, sid)
    _propose(client, sid, evidence=_evidence(breaker))
    waiting = reviewer_client.get("/api/dispositions/queue").json()["waiting"]
    assert [w["attemptId"] for w in waiting] == [sid]
    assert waiting[0]["annotator"] == "test@deccan.ai"
    assert waiting[0]["environmentImageDigest"] == "sha256:envA"

    assert _decide(reviewer_client, sid, note="reproduced").status_code == 200
    assert reviewer_client.get("/api/dispositions/queue").json()["waiting"] == []


def test_the_whole_surface_requires_authentication(anon_client):
    sid = uuid4()
    for path, method in [
        (f"/api/sessions/{sid}/disposition", "get"),
        (f"/api/sessions/{sid}/disposition", "post"),
        (f"/api/sessions/{sid}/disposition/decision", "post"),
        ("/api/dispositions/queue", "get"),
        ("/api/dispositions/summary", "get"),
    ]:
        r = anon_client.post(path, json={}) if method == "post" else anon_client.get(path)
        assert r.status_code == 401, path


def test_a_ruling_after_rework_still_closes_the_door(client, reviewer_client, breaker):
    """Regression. The AlreadyAdjudicated guard lets a re-proposal through while
    rework is outstanding — correctly, since the reviewer asked for it. But a
    ruling ANSWERS that request, and accept/reject used to leave the flag set, so
    the door stayed open forever and the annotator could overwrite the reviewer's
    ruling. That makes adjudication decorative, which is the one thing this
    module exists to prevent."""
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    rework = _decide(reviewer_client, sid, decision="request_rework", note="attach the dom dump").json()
    assert rework["reworkStatus"] == "requested"

    ruled = _decide(reviewer_client, sid, note="seen enough", expectedRevision=rework["revision"])
    assert ruled.status_code == 200, ruled.text
    assert ruled.json()["standing"] == "adjudicated"
    assert ruled.json()["reworkStatus"] == "", "the ruling answered the rework request"

    r = _propose(client, sid, evidence=_evidence(breaker), expectedRevision=ruled.json()["revision"])
    assert r.status_code == 409


def test_a_reject_after_rework_also_closes_the_door(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, disposition="environment_broken", evidence=_evidence(breaker))
    rework = _decide(reviewer_client, sid, decision="request_rework", note="need more").json()
    ruled = _decide(reviewer_client, sid, decision="reject", disposition="model_failure",
                    note="the env was fine; the agent gave up", expectedRevision=rework["revision"])
    assert ruled.status_code == 200, ruled.text
    assert ruled.json()["reworkStatus"] == ""
    assert _propose(client, sid, evidence=_evidence(breaker),
                    expectedRevision=ruled.json()["revision"]).status_code == 409


def test_overturned_evidence_is_not_shown_as_backing_the_ruling(client, reviewer_client, breaker):
    """Evidence belongs to the CLAIM it was gathered for. Showing the annotator's
    'the environment was broken' artifacts against a reviewer's 'the model failed'
    ruling presents evidence collected to argue one thing as if it backed the
    opposite — in the one report that exists to tell those two apart."""
    sid = _open(client, breaker.external_id)
    _propose(client, sid, disposition="environment_broken", evidence=_evidence(breaker))
    mine = client.get(f"/api/sessions/{sid}/disposition").json()
    assert mine["evidence"], "the annotator's own claim keeps its evidence"

    ruled = _decide(reviewer_client, sid, decision="reject", disposition="model_failure",
                    note="the env was fine; the agent gave up", expectedRevision=mine["revision"]).json()
    assert ruled["disposition"] == "model_failure"
    assert ruled["evidence"] == [], "the overturned claim's artifacts must not read as support"
    # …and they are not lost — the ledger still holds them under the proposal.
    proposed = [h for h in ruled["history"] if h["event"] == "proposed"][-1]
    assert proposed["evidence"], "the artifacts survive on the claim they were gathered for"


def test_an_accepted_proposal_keeps_its_evidence(client, reviewer_client, breaker):
    """Nobody overturned it, so the artifacts do back what stands."""
    sid = _open(client, breaker.external_id)
    _propose(client, sid, disposition="environment_broken", evidence=_evidence(breaker))
    out = _decide(reviewer_client, sid, note="reproduced").json()
    assert out["disposition"] == "environment_broken" and out["evidence"]


def test_the_reviewer_queue_holds_only_work_a_reviewer_can_do(client, reviewer_client, breaker, db_session):
    """A queue that cannot be emptied stops being read. A rework request IS the
    reviewer's action — until the annotator answers it, the ball is theirs."""
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    assert [w["attemptId"] for w in reviewer_client.get("/api/dispositions/queue").json()["waiting"]] == [sid]

    out = _decide(reviewer_client, sid, decision="request_rework", note="attach the dom dump").json()
    assert reviewer_client.get("/api/dispositions/queue").json()["waiting"] == [], (
        "the reviewer already acted; it is the annotator's turn"
    )

    # …and it comes back the moment the annotator answers.
    _propose(client, sid, evidence=_evidence(breaker), note="dom dump attached",
             expectedRevision=out["revision"])
    assert [w["attemptId"] for w in reviewer_client.get("/api/dispositions/queue").json()["waiting"]] == [sid]


def test_an_adjudicated_attempt_leaves_the_queue(client, reviewer_client, breaker):
    sid = _open(client, breaker.external_id)
    _propose(client, sid, evidence=_evidence(breaker))
    _decide(reviewer_client, sid, note="reproduced")
    assert reviewer_client.get("/api/dispositions/queue").json()["waiting"] == []
