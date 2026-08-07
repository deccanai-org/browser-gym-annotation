"""Sample packaging / export — the deliverable golden bundle."""

import json


def _golden(client, task="GYM-2041"):
    """Drive a full annotation: broken run → correct → golden → submit."""
    sid = client.post(f"/api/tasks/{task}/sessions", json={"annotatorEmail": "e@x.io", "fresh": True}).json()["sessionId"]
    # correct the broken step → persists the golden-tail branch
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    vs = client.get(f"/api/tasks/{task}/review").json()["verifiers"]
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False})
    return sid


def test_export_sample_is_a_complete_golden_bundle(client, reviewer_client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _golden(client)
    b = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert b["sample_id"] == sid
    assert b["task"]["id"] == "GYM-2041"
    assert b["reward"] == 1
    # GYM-2041 is a fixture with no captured seed world, so the eval triplet has
    # no first leg — and the bundle has to SAY so. It used to substitute the
    # non-world keys of seed_state ({"startState": …} here, {"initial_url",
    # "category", "difficulty"} for a gym task), which reads as a real world.
    assert b["initial_state"] is None
    assert b["initial_state_provenance"]["source"] is None
    assert b["initial_state_provenance"]["missing_reason"]
    assert b["correction"] and b["correction"]["from_step"] == 12
    assert len(b["golden_trajectory"]) > 0                      # the SFT trajectory
    assert len(b["verifiers"]) == 14                           # the verifier suite travels with it
    assert b["submission"]["reward"] == 1 and b["submission"]["kind"] == "golden"


def test_a_captured_seed_world_ships_as_the_initial_state(client, reviewer_client, db_session, monkeypatch):
    """The other half of the rule above: when a world HAS been captured it is the
    initial_state verbatim, and the bundle names where it came from. Without the
    source a consumer cannot tell a real world from the metadata stub the old
    fallback produced."""
    from sqlalchemy import select

    from app.models import Task

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    task = db_session.scalar(select(Task).where(Task.external_id == "GYM-2041"))
    task.seed_state = {"initial_url": "/shop", "world": {"shop": {"cart": []}}}
    db_session.commit()

    sid = _golden(client)
    b = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert b["initial_state"] == {"shop": {"cart": []}}
    assert b["initial_state_provenance"]["source"] == "task.seed_state.world"
    assert b["initial_state_provenance"]["missing_reason"] is None
    # the non-world metadata is still shipped, just not in the slot a client
    # resets from
    assert b["initial_state_provenance"]["metadata"]["initial_url"] == "/shop"


def test_the_exported_seed_is_the_one_the_annotation_ran_under(client, reviewer_client, db_session, monkeypatch):
    """The bundle used to ship `task.seed` — a mutable column a later capture or
    reseed rewrites. A client resetting at it would get a world the golden was
    never recorded in, so the seed has to come from the ATTEMPT."""
    from sqlalchemy import select

    from app.models import ReviewSession, Task

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _golden(client)
    attempt = db_session.get(ReviewSession, __import__("uuid").UUID(sid))
    recorded_seed = attempt.seed
    db_session.scalar(select(Task).where(Task.external_id == "GYM-2041")).seed = recorded_seed + 41
    db_session.commit()

    b = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert b["task"]["seed"] == recorded_seed


def test_export_dataset_jsonl(client, reviewer_client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _golden(client)
    r = reviewer_client.get("/api/export/dataset.jsonl")
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers["content-type"]
    lines = [ln for ln in r.text.strip().split("\n") if ln]
    assert len(lines) >= 1
    rec = json.loads(lines[0])
    assert {"task", "initial_state", "golden_trajectory", "verifiers", "reward"} <= set(rec)


def test_every_dataset_line_names_its_own_schema(client, reviewer_client, db_session, monkeypatch):
    """The JSONL interleaves two incompatible bundle shapes and only the
    version-bound one carried a `schema` key, so a loader either crashed on the
    keys a legacy row does not have or read a thin row as a full one. Every line
    must be self-describing."""
    from uuid import uuid4

    from sqlalchemy import select

    from app.models import ReviewSession, Submission, Task

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _golden(client)                                   # the legacy shape
    # …and a version-bound submission alongside it, which is how the two shapes
    # end up interleaved on one file.
    task = db_session.scalar(select(Task).where(Task.external_id == "GYM-2041"))
    versioned = ReviewSession(task_id=task.id, source="gym")
    db_session.add(versioned)
    db_session.flush()
    db_session.add(Submission(session_id=versioned.id, reward=1, kind="golden", snapshot={
        "trajectory_version": {"id": str(uuid4()), "versionNo": 1, "kind": "agent_run", "lineage": []},
        "verifiers": [], "golden_trajectory": [], "reward": 1,
    }))
    db_session.commit()

    lines = [json.loads(ln) for ln in reviewer_client.get("/api/export/dataset.jsonl").text.strip().split("\n") if ln]
    assert len(lines) >= 2
    assert all(rec.get("schema") for rec in lines)
    # and the discriminator actually discriminates: the versioned shape's keys
    # are absent from the legacy one, which is why guessing by key failed.
    for rec in lines:
        assert ("trajectory_version" in rec) == (rec["schema"].startswith("golden-sample/"))


def test_the_per_verifier_results_ship_with_the_reward(client, reviewer_client, monkeypatch):
    """A bare 0/1 does not say WHICH assertion a breaker broke — the only part of
    a failing sample a buyer can act on. The outcomes were frozen at submit and
    then dropped on the floor by export."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _golden(client)
    b = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert b["verifier_results"], "the frozen per-check outcomes must ship"
    assert set(b["verifier_results"].values()) <= {"pass", "fail"}
    assert all(v["id"] for v in b["verifiers"]), "a result is unattributable without the check id"
    assert {v["id"]: v["result"] for v in b["verifiers"]} == b["verifier_results"]


def test_list_samples_and_accepted_filter(client, reviewer_client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _golden(client)
    assert reviewer_client.get("/api/export/samples").json()["count"] >= 1
    # nothing accepted yet
    assert reviewer_client.get("/api/export/samples?accepted=true").json()["count"] == 0
    reviewer_client.post("/api/qa/tasks/GYM-2041/adjudicate", json={"sessionId": sid})
    assert reviewer_client.get("/api/export/samples?accepted=true").json()["count"] == 1


def test_export_reads_the_frozen_snapshot_not_the_live_suite(client, reviewer_client, monkeypatch, db_session):
    """Cluster A: the deliverable is frozen at submit. Even if a later suite
    version is forced into the DB directly (bypassing the API lock), the exported
    bundle must still reflect what was reviewed and scored at submit time."""
    from uuid import UUID

    from app.models import Verifier, VerifierSuite

    sid = _golden(client)
    before = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert before["reward"] == 1 and len(before["verifiers"]) == 14

    # Forge a bogus 1-verifier suite straight into the DB (what the closed lock now
    # prevents over the API, but proves build_sample ignores the live latest).
    forged = VerifierSuite(session_id=UUID(sid), version=99)
    db_session.add(forged)
    db_session.flush()
    db_session.add(Verifier(suite_id=forged.id, ext_id="bogus", level="ui", assertion="trivially true", code="x", check_ir={"kind": "state_true", "path": "order.placed"}))
    db_session.commit()

    after = reviewer_client.get(f"/api/export/samples/{sid}").json()
    assert after["reward"] == 1                     # unchanged — read from the snapshot
    assert len(after["verifiers"]) == 14            # not the forged single verifier
    assert not any(v["assertion"] == "trivially true" for v in after["verifiers"])


def test_gym_sample_exports_the_reviewed_trajectory(client, reviewer_client, db_session):
    """A GYM session owns no Trajectory row — it reviews the shared canonical run.
    Export must resolve that run, or every gym sample ships with an empty
    recorded_trajectory and a golden that is empty (or just the correction tail
    starting at a non-zero index). This is the dataset-integrity regression."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_gym import _synthetic_gym_review
    from app.api.gym import _persist_gym_review

    task_id = "M91/export_integrity"
    run, review = _synthetic_gym_review(task_id, success=False)   # 3-step breaking run
    _persist_gym_review(db_session, task_id, "openai", run, review)

    # the human reviews it in their OWN session (which has no Trajectory of its own)
    sid = client.post(f"/api/tasks/{task_id}/sessions", json={"fresh": True}).json()["sessionId"]
    # correct at step 1 → the branch carries the corrected tail
    client.post(f"/api/sessions/{sid}/rerun-gym", json={
        "fromStep": 1, "mode": "agent", "correction": "verify before acting",
        "steps": [{"idx": 2, "type": "click", "tabId": "shop", "description": "corrected tail"}],
    })
    vs = [{"id": v["id"], "level": v["level"], "assertion": v["assertion"], "code": v["code"],
           "check": None, "failsUntilCorrected": False, "placeholder": False,
           "addedByHuman": False, "gymResult": v.get("gymResult")} for v in review["verifiers"]]
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": [], "overrides": []})
    client.post(f"/api/sessions/{sid}/submit", json={
        "reward": 0, "override": True, "overrideReason": "confirmed breaker", "kind": "breaker"})

    b = reviewer_client.get(f"/api/export/samples/{sid}").json()
    # the run under review must actually ship
    assert len(b["recorded_trajectory"]) == 3, b["recorded_trajectory"]
    assert [st["idx"] for st in b["recorded_trajectory"]] == [0, 1, 2]
    # golden = canonical prefix (idx <= fromStep) + the corrected tail, not the tail alone
    assert len(b["golden_trajectory"]) == 3, b["golden_trajectory"]
    assert b["golden_trajectory"][0]["idx"] == 0, "golden must start at the beginning, not mid-run"
    assert b["golden_trajectory"][-1]["description"] == "corrected tail"
    assert b["correction"]["from_step"] == 1


# --- the reviewer gate ------------------------------------------------------

def test_an_annotator_cannot_list_the_cohorts_samples(client_for):
    """/samples enumerates every annotator's submission and its reward."""
    assert client_for("nosy@x.io").get("/api/export/samples").status_code == 403


def test_an_annotator_cannot_download_another_annotators_bundle(client, client_for, monkeypatch):
    """The bundle is the complete frozen submission — trajectory, verifiers and
    all. The QA panel's download link already pointed at this, so it was readable
    by anyone signed in."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _golden(client)
    assert client_for("nosy@x.io").get(f"/api/export/samples/{sid}").status_code == 403
