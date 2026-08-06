"""The annotator's board — the five states and what they mean to the person.

This endpoint had no tests at all, and it is what an annotator looks at every
day. The states it reports are not cosmetic: "waiting on a reviewer" and "done"
feel nothing alike, and "sent back" is the only one that is actionable.

The case that matters most is the returned one, because it spans three modules —
QA writes `rework_status`, the board reads it, and opening the task clears it —
and a mistake anywhere in that chain shows up as an annotator who is either never
told to redo the work or told forever.
"""

from __future__ import annotations

import pytest


def _gym_task(db_session, ext: str, in_85: bool = True):
    from app import models

    t = models.Task(external_id=ext, title=ext, prompt="p", source="gym",
                    meta={"inEightyFive": in_85, "primaryApp": "shop"})
    db_session.add(t)
    db_session.commit()
    return t


def _submit(client, db_session, task_ext: str) -> str:
    """Open a task and put it in the submitted state.

    The submission is written directly rather than driven through
    run + submit: those need a canonical agent run this ad-hoc task has none of,
    and the pipeline is already covered by test_sessions. What is under test here
    is what the BOARD makes of a submitted sample.
    """
    from uuid import UUID

    from app import models

    sid = client.post(f"/api/tasks/{task_ext}/sessions", json={"fresh": True}).json()["sessionId"]
    db_session.expire_all()
    s = db_session.get(models.ReviewSession, UUID(sid))
    s.status = "submitted"
    db_session.add(models.Submission(session_id=s.id, reward=0, kind="breaker",
                                     submitted_with_override=True, override_reason="confirmed breaker"))
    db_session.commit()
    return sid


def _board(client) -> dict:
    return client.get("/api/my-tasks").json()


def _row(board: dict, ext: str) -> dict:
    return next(r for r in board["tasks"] if r["id"] == ext)


@pytest.fixture()
def gym_task(db_session):
    return _gym_task(db_session, "MT-BOARD/one")


# --------------------------------------------------------------------- states

def test_an_untouched_task_is_to_do(client, gym_task):
    assert _row(_board(client), gym_task.external_id)["status"] == "todo"


def test_an_open_session_is_in_progress(client, gym_task):
    client.post(f"/api/tasks/{gym_task.external_id}/sessions", json={"fresh": True})
    assert _row(_board(client), gym_task.external_id)["status"] == "in_progress"


def test_a_submitted_sample_waits_in_review_until_someone_rules_on_it(client, gym_task, db_session, monkeypatch):
    """It used to read as "submitted" the moment it was sent, which told the
    annotator they were done while the sample had not been looked at."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _submit(client, db_session, gym_task.external_id)
    assert _row(_board(client), gym_task.external_id)["status"] == "in_review"


def test_only_an_accepted_sample_counts_as_submitted(client, reviewer_client, gym_task, db_session, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _submit(client, db_session, gym_task.external_id)
    reviewer_client.post(f"/api/qa/tasks/{gym_task.external_id}/adjudicate", json={"sessionId": sid})
    assert _row(_board(client), gym_task.external_id)["status"] == "submitted"


def test_two_annotators_on_the_same_task_see_their_own_progress(client_for, gym_task):
    a, b = client_for("board-a@x.io"), client_for("board-b@x.io")
    a.post(f"/api/tasks/{gym_task.external_id}/sessions", json={"fresh": True})
    assert _row(_board(a), gym_task.external_id)["status"] == "in_progress"
    assert _row(_board(b), gym_task.external_id)["status"] == "todo"


# ------------------------------------------------------------------- returned

def _return_it(reviewer_client, client, task_ext, note="the cart was left with an extra item"):
    subs = reviewer_client.get(f"/api/qa/tasks/{task_ext}/submissions").json()["submissions"]
    return reviewer_client.post(f"/api/qa/submissions/{subs[0]['submissionId']}/return",
                                json={"note": note})


def test_a_returned_sample_shows_as_returned_with_the_reason(
        client, reviewer_client, gym_task, db_session, monkeypatch):
    """The whole point of the loop: the annotator is told, and told WHY."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _submit(client, db_session, gym_task.external_id)
    assert _return_it(reviewer_client, client, gym_task.external_id).status_code == 200

    row = _row(_board(client), gym_task.external_id)
    assert row["status"] == "returned"
    assert "extra item" in row["reworkNote"]


def test_returned_work_is_offered_first(client, reviewer_client, db_session, monkeypatch):
    """It is blocking a reviewer and it already says what to do."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    done = _gym_task(db_session, "MT-BOARD/returned")
    _gym_task(db_session, "MT-BOARD/untouched")
    _submit(client, db_session, done.external_id)
    _return_it(reviewer_client, client, done.external_id)

    assert _board(client)["nextUp"]["id"] == done.external_id


def test_returning_does_not_unlock_the_frozen_submission(
        client, reviewer_client, gym_task, db_session, monkeypatch):
    """The exported sample must keep describing what was actually reviewed. The
    redo is a new attempt, not an edit of the old one."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _submit(client, db_session, gym_task.external_id)
    _return_it(reviewer_client, client, gym_task.external_id)

    r = client.put(f"/api/sessions/{sid}/suite", json={"verifiers": []})
    assert r.status_code == 409, "a submitted attempt stays frozen after it is returned"


def test_reopening_a_returned_task_links_back_and_clears_the_flag(
        client, reviewer_client, gym_task, db_session, monkeypatch):
    """Without the link the two attempts are unrelated rows and nobody can tell a
    rework from a second opinion; without clearing it the board says Returned
    forever."""
    from app import models

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    first = _submit(client, db_session, gym_task.external_id)
    _return_it(reviewer_client, client, gym_task.external_id)

    second = client.post(f"/api/tasks/{gym_task.external_id}/sessions",
                         json={"fresh": True}).json()["sessionId"]
    assert second != first

    db_session.expire_all()
    old = db_session.get(models.ReviewSession, __import__("uuid").UUID(first))
    new = db_session.get(models.ReviewSession, __import__("uuid").UUID(second))
    assert old.rework_status == "done", "the request has been answered"
    assert str(new.origin_session_id) == first, "the rework points back at what it replaces"
    assert _row(_board(client), gym_task.external_id)["status"] == "in_progress"


# ---------------------------------------------------------------------- quota

def test_quota_counts_the_work_the_annotator_finished(
        client, reviewer_client, gym_task, db_session, monkeypatch):
    """Counting only ADJUDICATED samples made an annotator's number fall whenever
    a reviewer was behind — work they had already done, un-done on their own
    board. `accepted` is reported separately so the backlog is still visible."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _submit(client, db_session, gym_task.external_id)

    q = _board(client)["quota"]
    assert q["submitted"] == 1, "finished work counts the moment it is submitted"
    assert q["accepted"] == 0, "and the review backlog is still visible"

    reviewer_client.post(f"/api/qa/tasks/{gym_task.external_id}/adjudicate", json={"sessionId": sid})
    q = _board(client)["quota"]
    assert q["submitted"] == 1 and q["accepted"] == 1


# ------------------------------------------------------------- the return gate

def test_a_return_needs_a_reason(client, reviewer_client, gym_task, db_session, monkeypatch):
    """"Do it again" with no reason is not a review — the annotator cannot act."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _submit(client, db_session, gym_task.external_id)
    subs = reviewer_client.get(f"/api/qa/tasks/{gym_task.external_id}/submissions").json()["submissions"]
    r = reviewer_client.post(f"/api/qa/submissions/{subs[0]['submissionId']}/return",
                             json={"note": "   "})
    assert r.status_code == 422


def test_an_annotator_cannot_return_a_submission(client, client_for, gym_task, db_session, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _submit(client, db_session, gym_task.external_id)
    r = client_for("nosy@x.io").post(
        "/api/qa/submissions/00000000-0000-0000-0000-000000000000/return", json={"note": "no"})
    assert r.status_code == 403


def test_a_reviewer_cannot_send_their_own_attempt_back(reviewer_client, gym_task, db_session, monkeypatch):
    """Self-review would make the loop decorative."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    _submit(reviewer_client, db_session, gym_task.external_id)
    subs = reviewer_client.get(f"/api/qa/tasks/{gym_task.external_id}/submissions").json()["submissions"]
    r = reviewer_client.post(f"/api/qa/submissions/{subs[0]['submissionId']}/return",
                             json={"note": "not good enough"})
    assert r.status_code == 409


def test_the_board_follows_the_newest_attempt_not_the_newest_write(
        client, reviewer_client, gym_task, db_session, monkeypatch):
    """Answering a rework request writes `rework_status = "done"` on the OLD
    session, which moves its `updated_at` past the fresh attempt's. Ordering the
    board by last-write therefore left it showing the returned attempt after the
    annotator had already started the redo — they were told to fix something they
    were in the middle of fixing.

    Forced here rather than left to timestamp luck: the two writes land in the
    same commit, so which one looks newer is otherwise a coin flip.
    """
    from datetime import datetime, timedelta
    from uuid import UUID

    from app import models

    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    first = _submit(client, db_session, gym_task.external_id)
    _return_it(reviewer_client, client, gym_task.external_id)
    second = client.post(f"/api/tasks/{gym_task.external_id}/sessions",
                         json={"fresh": True}).json()["sessionId"]

    # the returned attempt is the most recently WRITTEN row...
    db_session.expire_all()
    old = db_session.get(models.ReviewSession, UUID(first))
    new = db_session.get(models.ReviewSession, UUID(second))
    old.updated_at = datetime.utcnow() + timedelta(minutes=5)
    new.created_at = old.created_at + timedelta(seconds=1)
    db_session.commit()

    # ...but the newest ATTEMPT is what the annotator is doing
    assert _row(_board(client), gym_task.external_id)["status"] == "in_progress"
