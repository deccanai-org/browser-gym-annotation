"""The generated verifier suite — cached, and actually usable.

The oracle loop worked and threw its result away: it returned the suite as JSON,
nothing persisted it, and no attempt could reach a reward from it without a human
retyping every check. Step 2 of the review screen was decorative for the whole
pilot.

What these lock: the suite survives the job, a second annotator on the same
breaker does not pay for the loop again, and adopting it writes the same rows an
annotator's own save would — with the provenance that says a model wrote them.
"""

from __future__ import annotations

import pytest


CHECKS = [
    {"id": "order_placed", "level": "backend", "assertion": "an order was placed",
     "code": "orders>=1", "check": {"kind": "state_len_gte", "path": "shop.orders", "value": 1}},
    {"id": "cart_emptied", "level": "backend", "assertion": "the cart is empty",
     "code": "cart==0", "check": {"kind": "state_empty", "path": "shop.cart.items"}},
    {"id": "p1", "level": "safety", "assertion": "never buys anything extra",
     "code": "policy: never buys anything extra",
     "check": {"kind": "trace_policy", "policy": "never buys anything extra"}},
]


@pytest.fixture()
def task(db_session):
    from uuid import uuid4

    from app import models

    t = models.Task(external_id=f"AG-{uuid4().hex[:6]}/x", title="t", prompt="p", source="gym", seed=0)
    db_session.add(t)
    db_session.commit()
    return t


def _cache(db, task_ext: str, seed: int = 0, oracle: bool = True, checks=None):
    from app.api.gym import _write_autogen_suite

    _write_autogen_suite(db, task_ext, seed, {
        "oracle": oracle, "brief": "place the order", "checks": checks or CHECKS,
        "gate": {"oracle": oracle, "initialReward": 0, "goldenReward": 1}, "iterations": 2,
    })


# ------------------------------------------------------------------ the cache

def test_a_generated_suite_survives_the_job(db_session, task):
    """It used to be returned as JSON and dropped on the floor."""
    from app import models

    _cache(db_session, task.external_id)
    db_session.expire_all()
    row = db_session.query(models.AutogenSuite).filter_by(task_id=task.id).one()
    assert row.oracle is True
    assert [c["id"] for c in row.checks] == ["order_placed", "cart_emptied", "p1"]


def test_regenerating_replaces_rather_than_duplicates(db_session, task):
    """Regenerating is how a human improves a suite that failed its gate. Two rows
    would leave two answers to "what is the suite for this task"."""
    from app import models

    _cache(db_session, task.external_id, oracle=False, checks=CHECKS[:1])
    _cache(db_session, task.external_id, oracle=True, checks=CHECKS)
    db_session.expire_all()
    rows = db_session.query(models.AutogenSuite).filter_by(task_id=task.id).all()
    assert len(rows) == 1
    assert rows[0].oracle is True and len(rows[0].checks) == 3


def test_a_different_seed_is_a_different_suite(db_session, task):
    """A different seed is a different pair of worlds, so the checks that
    discriminate them are different checks."""
    from app import models

    _cache(db_session, task.external_id, seed=0)
    _cache(db_session, task.external_id, seed=1)
    db_session.expire_all()
    assert db_session.query(models.AutogenSuite).filter_by(task_id=task.id).count() == 2


def test_the_cached_suite_can_be_read_back(client, db_session, task):
    _cache(db_session, task.external_id)
    r = client.get(f"/api/gym/tasks/{task.external_id}/verifier-suite")
    assert r.status_code == 200
    body = r.json()
    assert body["oracle"] is True and len(body["checks"]) == 3


def test_a_task_with_no_generated_suite_says_so(client, task):
    """404, not an empty suite — "none yet" and "an empty one" are different
    answers and the screen offers different things for each."""
    assert client.get(f"/api/gym/tasks/{task.external_id}/verifier-suite").status_code == 404


# ------------------------------------------------------------------- adopting

def _open(client, task_ext: str) -> str:
    return client.post(f"/api/tasks/{task_ext}/sessions", json={"fresh": True}).json()["sessionId"]


def test_adopting_writes_a_real_suite_the_reward_can_read(client, db_session, task):
    from uuid import UUID

    from app import models

    _cache(db_session, task.external_id)
    sid = _open(client, task.external_id)
    r = client.post(f"/api/sessions/{sid}/suite/from-autogen", json={})
    assert r.status_code == 200, r.json()

    db_session.expire_all()
    suite = db_session.query(models.VerifierSuite).filter_by(session_id=UUID(sid)).one()
    got = {v.ext_id: v for v in suite.verifiers}
    assert set(got) == {"order_placed", "cart_emptied", "p1"}
    # the executable IR travels, or the reward cannot be recomputed server-side
    assert got["order_placed"].check_ir["kind"] == "state_len_gte"
    assert got["p1"].level == "safety"


def test_an_adopted_check_is_not_credited_to_a_human(client, db_session, task):
    """The exported sample must say which checks a person wrote and which a model
    did — that provenance is the difference between human ground truth and a model
    grading itself."""
    from uuid import UUID

    from app import models

    _cache(db_session, task.external_id)
    sid = _open(client, task.external_id)
    client.post(f"/api/sessions/{sid}/suite/from-autogen", json={})
    db_session.expire_all()
    suite = db_session.query(models.VerifierSuite).filter_by(session_id=UUID(sid)).one()
    assert all(v.added_by_human is False for v in suite.verifiers)


def test_a_second_annotator_reuses_it_without_running_the_loop_again(client_for, db_session, task):
    """The loop costs an oracle run plus several model calls. It is a property of
    (task, seed), so the second person on a breaker should pay nothing."""
    _cache(db_session, task.external_id)
    a, b = client_for("ag-a@x.io"), client_for("ag-b@x.io")
    for c in (a, b):
        sid = _open(c, task.external_id)
        assert c.post(f"/api/sessions/{sid}/suite/from-autogen", json={}).status_code == 200


def test_adopting_with_nothing_generated_says_what_to_do(client, task):
    sid = _open(client, task.external_id)
    r = client.post(f"/api/sessions/{sid}/suite/from-autogen", json={})
    assert r.status_code == 409
    assert "Auto-generate" in r.json()["detail"]


def test_adopting_twice_versions_the_suite_rather_than_colliding(client, db_session, task):
    from uuid import UUID

    from app import models

    _cache(db_session, task.external_id)
    sid = _open(client, task.external_id)
    first = client.post(f"/api/sessions/{sid}/suite/from-autogen", json={}).json()
    second = client.post(f"/api/sessions/{sid}/suite/from-autogen", json={}).json()
    assert second["version"] == first["version"] + 1
    db_session.expire_all()
    assert db_session.query(models.VerifierSuite).filter_by(session_id=UUID(sid)).count() == 2


def test_a_submitted_attempt_will_not_take_a_new_suite(client, db_session, task):
    """The frozen snapshot is what makes an exported sample trustworthy."""
    from uuid import UUID

    from app import models

    _cache(db_session, task.external_id)
    sid = _open(client, task.external_id)
    db_session.expire_all()
    db_session.get(models.ReviewSession, UUID(sid)).status = "submitted"
    db_session.commit()
    assert client.post(f"/api/sessions/{sid}/suite/from-autogen", json={}).status_code == 409


def test_another_annotators_attempt_is_not_yours_to_change(client_for, db_session, task):
    _cache(db_session, task.external_id)
    a, b = client_for("ag-owner@x.io"), client_for("ag-other@x.io")
    sid = _open(a, task.external_id)
    # 404 not 403 — whether that attempt exists is not b's business
    assert b.post(f"/api/sessions/{sid}/suite/from-autogen", json={}).status_code == 404


def test_an_empty_suite_cannot_be_saved_over_a_good_one(db_session, task):
    """Suite versions are immutable and the ship gate takes the NEWEST.

    So saving an empty one is quietly destructive: it shadows a good suite, and
    every attempt to ship afterwards fails with "no verifier that proves
    anything" while the real suite sits one version below, intact and
    unreachable. Found on a real M105 attempt — v1 held all three gym
    milestones, v2 held nothing, and only naming v1's id explicitly could ship
    it.
    """
    from fastapi import HTTPException

    from app import models
    from app.api.sessions import write_suite

    s = models.ReviewSession(task_id=task.id, seed=0, status="draft")
    db_session.add(s)
    db_session.commit()

    good = write_suite(db_session, s.id, [{"id": "m0", "level": "backend",
                                           "assertion": "order placed",
                                           "check": {"kind": "gym_milestone", "id": "m0"}}])
    db_session.commit()

    with pytest.raises(HTTPException) as caught:
        write_suite(db_session, s.id, [])
    assert caught.value.status_code == 422
    assert "no verifiers" in str(caught.value.detail)

    db_session.rollback()
    latest = (db_session.query(models.VerifierSuite)
              .filter_by(session_id=s.id).order_by(models.VerifierSuite.version.desc()).first())
    assert latest.id == good.id, "the good suite must still be the newest"


def test_a_pre_existing_empty_suite_does_not_shadow_the_good_one(db_session, task):
    """The guard stops NEW empty suites; rows written before it are still there.

    Versions are immutable and selection is by version DESC, which is what
    finalize uses when no suite id is named — so an empty v2 sitting above a good
    v1 makes the attempt unshippable unless the caller knows to name v1 by hand.
    Measured on a real shipped M105 attempt: v1 held all three gym milestones,
    v2 held nothing.
    """
    from app import models
    from app.api.sessions import _latest_suite, write_suite

    s = models.ReviewSession(task_id=task.id, seed=0, status="draft")
    db_session.add(s)
    db_session.commit()

    good = write_suite(db_session, s.id, [{"id": "m0", "level": "backend",
                                           "assertion": "order placed",
                                           "check": {"kind": "gym_milestone", "id": "m0"}}])
    # An empty v2 as the legacy rows have it — written directly, since the guard
    # now refuses this path.
    db_session.add(models.VerifierSuite(session_id=s.id, version=good.version + 1))
    db_session.commit()

    picked = _latest_suite(db_session, s.id)

    assert picked is not None and picked.id == good.id, (
        "an empty suite proves nothing and must never be the attempt's suite"
    )
    assert len(picked.verifiers) == 1
