"""Who is meant to annotate which task.

"Assigned" used to mean every gym task carrying `meta.inEightyFive`, so every
annotator saw the same 85 rows and every quota target was 85. With several people
on the batch nobody could tell what was theirs, and the progress bar measured the
cohort rather than the person.

The case worth protecting is the fallback: assignments are new, and an annotator
whose rows have not been written yet must not open the app to an empty board and
conclude the work is gone.
"""

from __future__ import annotations

import pytest


def _gym_task(db, ext: str, in_85: bool = True):
    from app import models

    t = models.Task(external_id=ext, title=ext, prompt="p", source="gym",
                    meta={"inEightyFive": in_85, "primaryApp": "shop"})
    db.add(t)
    db.commit()
    return t


@pytest.fixture()
def tasks(db_session):
    return [_gym_task(db_session, f"AS-{i}/x") for i in range(6)]


def _assign(reviewer_client, emails, **kw):
    return reviewer_client.post("/api/admin/assign",
                                json={"annotatorEmails": emails, **kw})


def _board(client):
    return client.get("/api/my-tasks").json()


# ------------------------------------------------------------------ the board

def test_an_annotator_with_no_assignments_still_sees_the_batch(client, tasks):
    """The fallback. Rolling this out must not blank anybody's board — an empty
    one reads as "my work is gone", not as "nobody has assigned me yet"."""
    b = _board(client)
    assert len(b["tasks"]) >= len(tasks)
    assert "unassigned" in b["batch"], "and it says which of the two you are looking at"


def test_an_assigned_annotator_sees_only_their_own(client_for, reviewer_client, tasks, db_session):
    a, b = client_for("as-a@x.io"), client_for("as-b@x.io")   # both must exist first
    r = _assign(reviewer_client, ["as-a@x.io", "as-b@x.io"],
                taskIds=[t.external_id for t in tasks], batch="pilot-1")
    assert r.status_code == 200, r.json()

    ids_a = {t["id"] for t in _board(a)["tasks"]}
    ids_b = {t["id"] for t in _board(b)["tasks"]}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b), "round-robin at overlap 1 gives each task to one person"
    assert ids_a | ids_b == {t.external_id for t in tasks}


def test_the_quota_target_is_what_you_were_given(client_for, reviewer_client, tasks):
    """It used to be 85 for everyone, including someone assigned three tasks."""
    a = client_for("as-quota@x.io")
    client_for("as-other@x.io")            # both must exist to be assigned to
    _assign(reviewer_client, ["as-quota@x.io", "as-other@x.io"],
            taskIds=[t.external_id for t in tasks])
    board = _board(a)
    assert board["quota"]["target"] == len(board["tasks"]) == 3


def test_the_board_shows_the_batch_it_was_assigned_under(client_for, reviewer_client, tasks):
    a = client_for("as-batch@x.io")
    _assign(reviewer_client, ["as-batch@x.io"], taskIds=[tasks[0].external_id], batch="pilot-7")
    assert _board(a)["batch"] == "pilot-7"


# --------------------------------------------------------------- the assigning

def test_overlap_puts_several_annotators_on_one_task(client_for, reviewer_client, tasks):
    """Deliberate, not a bug: the QA agreement number only means something when
    more than one person has independently done the task."""
    a, b = client_for("ov-a@x.io"), client_for("ov-b@x.io")
    _assign(reviewer_client, ["ov-a@x.io", "ov-b@x.io"],
            taskIds=[t.external_id for t in tasks], overlap=2)
    ids_a = {t["id"] for t in _board(a)["tasks"]}
    ids_b = {t["id"] for t in _board(b)["tasks"]}
    assert ids_a == ids_b == {t.external_id for t in tasks}


def test_assigning_again_adds_nothing(client_for, reviewer_client, tasks):
    """Growing a batch means re-running the same command with one more name on
    it, so a repeat must not double everyone's board."""
    ids = [t.external_id for t in tasks]
    client_for("idem@x.io")
    first = _assign(reviewer_client, ["idem@x.io"], taskIds=ids).json()
    second = _assign(reviewer_client, ["idem@x.io"], taskIds=ids).json()
    assert first["created"] == len(ids)
    assert second["created"] == 0
    assert len(_board(client_for("idem@x.io"))["tasks"]) == len(ids)


def test_adding_a_person_to_an_existing_batch_only_writes_the_new_rows(
        client_for, reviewer_client, tasks):
    ids = [t.external_id for t in tasks]
    client_for("grow-a@x.io"); client_for("grow-b@x.io")
    _assign(reviewer_client, ["grow-a@x.io"], taskIds=ids)
    out = _assign(reviewer_client, ["grow-a@x.io", "grow-b@x.io"], taskIds=ids).json()
    assert 0 < out["created"] < len(ids) * 2


def test_the_split_does_not_depend_on_the_order_the_names_were_typed(
        client_for, reviewer_client, db_session):
    """Otherwise re-running the same command hands people different work."""
    from app import models

    ids = [_gym_task(db_session, f"ORD-{i}/x").external_id for i in range(4)]
    client_for("z@x.io"); client_for("a@x.io")
    _assign(reviewer_client, ["z@x.io", "a@x.io"], taskIds=ids)
    first = {t["id"] for t in _board(client_for("a@x.io"))["tasks"]}

    db_session.query(models.TaskAssignment).delete()
    db_session.commit()

    _assign(reviewer_client, ["a@x.io", "z@x.io"], taskIds=ids)
    assert {t["id"] for t in _board(client_for("a@x.io"))["tasks"]} == first


# --------------------------------------------------------------------- refusal

def test_an_annotator_cannot_assign_work(client_for, tasks):
    r = client_for("plain@x.io").post("/api/admin/assign",
                                      json={"annotatorEmails": ["plain@x.io"]})
    assert r.status_code == 403


def test_assigning_to_someone_who_does_not_exist_is_refused(reviewer_client, tasks):
    """Silently skipping them would leave a batch quietly short-staffed."""
    r = _assign(reviewer_client, ["ghost@x.io"])
    assert r.status_code == 404 and "ghost@x.io" in r.json()["detail"]


def test_an_unknown_task_is_refused(reviewer_client, client_for):
    client_for("real@x.io")
    r = _assign(reviewer_client, ["real@x.io"], taskIds=["NOPE/x"])
    assert r.status_code == 404


def test_overlap_beyond_the_cohort_is_refused(reviewer_client, client_for, tasks):
    """Asking for 3 people per task with 2 annotators would silently assign the
    same person twice — the unique key drops it and the batch is short."""
    client_for("one@x.io"); client_for("two@x.io")
    r = _assign(reviewer_client, ["one@x.io", "two@x.io"],
                taskIds=[t.external_id for t in tasks], overlap=3)
    assert r.status_code == 422


def test_assignment_does_not_gate_opening_a_task(client_for, reviewer_client, tasks):
    """The gym picker exists so anyone can explore the 312 deliberately. The
    board is the assignment surface; opening is not."""
    a = client_for("explorer@x.io")
    client_for("someone-else@x.io")
    _assign(reviewer_client, ["someone-else@x.io"], taskIds=[tasks[0].external_id])
    r = a.post(f"/api/tasks/{tasks[0].external_id}/sessions", json={"fresh": True})
    assert r.status_code == 200
