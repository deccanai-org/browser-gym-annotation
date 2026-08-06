"""Multi-annotator QA — agreement aggregation + reviewer adjudication.

Identity now comes from auth (a signed-in account per client), not a body email,
so each annotator is a separate authenticated client via `client_for`.

The whole QA surface is reviewer-only: it names every annotator and the reward
they submitted, so an annotator who could read it could align to the majority
before submitting — destroying the very agreement signal it measures.
"""


def _submit(c, task, corrected):
    sid = c.post(f"/api/tasks/{task}/sessions", json={"fresh": True}).json()["sessionId"]
    vs = c.get(f"/api/tasks/{task}/review").json()["verifiers"]
    if corrected:
        c.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "fix", "mode": "deterministic"})
    c.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    c.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    c.post(f"/api/sessions/{sid}/run", json={"corrected": corrected, "verifiers": vs, "overrides": []})
    c.post(f"/api/sessions/{sid}/submit", json={
        "reward": 1 if corrected else 0, "override": not corrected, "overrideReason": "x"})
    return sid


def test_qa_agreement_and_adjudication(client_for, reviewer_client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    ca, cb, cc = client_for("a@x.io"), client_for("b@x.io"), client_for("c@x.io")
    a = _submit(ca, "GYM-2041", True)
    _submit(cb, "GYM-2041", True)
    _submit(cc, "GYM-2041", False)  # a dissenting reward 0

    row = next(t for t in reviewer_client.get("/api/qa/tasks").json()["tasks"]
               if t["taskExternalId"] == "GYM-2041")
    assert row["submissions"] == 3 and row["annotators"] == 3
    assert row["majorityReward"] == 1 and row["disputed"] is True
    assert row["agreement"] == round(2 / 3, 3)

    r = reviewer_client.post("/api/qa/tasks/GYM-2041/adjudicate", json={"sessionId": a})
    assert r.status_code == 200
    subs = reviewer_client.get("/api/qa/tasks/GYM-2041/submissions").json()["submissions"]
    accepted = [s for s in subs if s["accepted"]]
    assert len(accepted) == 1 and accepted[0]["sessionId"] == a


def test_agreement_is_per_distinct_annotator_not_submission_count(client_for, reviewer_client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    # bob is prolific: 2 sessions, both reward 0. alice: 1 session, reward 1.
    cb, ca = client_for("bob@x.io"), client_for("alice@x.io")
    _submit(cb, "GYM-2041", False)
    _submit(cb, "GYM-2041", False)
    _submit(ca, "GYM-2041", True)
    row = next(t for t in reviewer_client.get("/api/qa/tasks").json()["tasks"]
               if t["taskExternalId"] == "GYM-2041")
    assert row["submissions"] == 3 and row["annotators"] == 2
    # per-annotator votes = {bob:0, alice:1} → agreement 0.5; submission-weighted would be 0.667.
    assert row["agreement"] == 0.5


def test_qa_unknown_task_404(reviewer_client):
    assert reviewer_client.get("/api/qa/tasks/NOPE/submissions").status_code == 404


# --- the reviewer gate ------------------------------------------------------

def test_an_annotator_cannot_read_the_cohorts_qa_board(client_for):
    """The board carries every annotator's reward for a task. Reading it before
    submitting would let someone match the majority instead of judging."""
    assert client_for("nosy@x.io").get("/api/qa/tasks").status_code == 403


def test_an_annotator_cannot_read_another_annotators_submissions(client_for):
    assert client_for("nosy@x.io").get("/api/qa/tasks/GYM-2041/submissions").status_code == 403


def test_an_annotator_cannot_adjudicate(client_for):
    """Accepting a golden sample is the reviewer's decision, not a peer's."""
    r = client_for("nosy@x.io").post("/api/qa/tasks/GYM-2041/adjudicate",
                                     json={"sessionId": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 403


def test_the_audit_actor_is_the_signed_in_reviewer_not_the_request_body(
        client_for, reviewer_client, monkeypatch):
    """The body used to carry `reviewer`, so a caller could sign someone else's
    name to the decision that picks the golden sample."""
    from sqlalchemy import select

    from app import models
    from app.db import SessionLocal

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _submit(client_for("a@x.io"), "GYM-2041", True)

    # An ignored `reviewer` key: even if a client sends one, it must not be used.
    r = reviewer_client.post("/api/qa/tasks/GYM-2041/adjudicate",
                             json={"sessionId": sid, "reviewer": "someone.else@evil.io"})
    assert r.status_code == 200

    with SessionLocal() as db:
        log = db.scalar(
            select(models.AuditLog)
            .where(models.AuditLog.action == "qa.adjudicate")
            .order_by(models.AuditLog.created_at.desc())
        )
    assert log is not None
    assert log.actor == "reviewer@deccan.ai"
