"""Which world an attempt resolves to — the multi-annotator isolation boundary.

This surface had no tests at all, and it is the one place where a mistake is
invisible in development (one annotator, one gym, everything looks fine) and
catastrophic in production (several annotators silently sharing a world).

The case that matters: a bridged attempt hands its pooled gym back when the
annotator closes the pane, so another annotator can work. If resolving its world
afterwards falls through to the SHARED gym, then finalize / certify / commit /
reset read and MUTATE somebody else's in-progress world.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import live_world, models


class _Attempt:
    """The few ReviewSession fields world_for actually reads."""

    def __init__(self, *, bridge_session_id="", bridge_gym_url="", cua_apps=None):
        self.id = uuid4()
        self.bridge_session_id = bridge_session_id
        self.bridge_gym_url = bridge_gym_url
        self.cua_apps = cua_apps


def _apps():
    return [{"app": "shop", "attempt_sid": str(uuid4()), "url": "http://localhost:5201/"}]


# --------------------------------------------------------------------------- leased


def test_a_bridged_attempt_resolves_to_the_gym_the_bridge_leased_it(db_session):
    a = _Attempt(bridge_session_id="sess-1", bridge_gym_url="http://127.0.0.1:8077", cua_apps=_apps())
    world = live_world.world_for(db_session, a)
    assert world.kind == "bridged"
    assert "8077" in world.gym.base_url, "the leased gym's PORT identifies the instance"


# --------------------------------------------------------------------------- released


def test_a_released_bridged_attempt_has_NO_world_rather_than_the_shared_one(db_session):
    """The regression this file exists for.

    Closing the pane releases the pooled gym and clears `bridge_session_id`.
    Before UnleasedWorld, `world_for` then returned the shared gym and every
    caller mutated it — with two annotators that is one annotator's finalize
    replaying into the other's cart.
    """
    a = _Attempt(bridge_session_id="", bridge_gym_url="", cua_apps=_apps())
    world = live_world.world_for(db_session, a)

    assert world.kind == "unleased"
    assert world.world() is None
    assert world.state() is None
    assert world.verify() is None
    # And it must not be mistakable for a real endpoint.
    assert world.kind != "workspace"


def test_the_release_is_keyed_on_a_durable_fact_not_an_env_flag(db_session):
    """`cua_apps` is written when the attempt first opens and outlives the lease.

    Keying on `cua_hub.enabled()` instead would make an attempt's world resolve
    differently under test than in production — the exact class of bug this
    module exists to prevent.
    """
    never_bridged = _Attempt(cua_apps=None)
    assert live_world.world_for(db_session, never_bridged).kind == "workspace", (
        "a plain attempt keeps the pre-existing workspace behaviour"
    )

    was_bridged = _Attempt(cua_apps=_apps())
    assert live_world.world_for(db_session, was_bridged).kind == "unleased"


# --------------------------------------------------------------------------- the API refuses


@pytest.fixture()
def released_attempt(client, db_session):
    """An attempt that opened a bridged world and then closed its pane."""
    task = models.Task(external_id=f"M99_rel_{uuid4().hex[:6]}", title="released",
                       prompt="do it", source="gym")
    db_session.add(task)
    db_session.commit()
    sid = client.post(f"/api/tasks/{task.external_id}/sessions", json={}).json()["sessionId"]

    s = db_session.get(models.ReviewSession, __import__("uuid").UUID(sid))
    s.cua_apps = _apps()          # it owns a bridged world…
    s.bridge_session_id = ""      # …but the gym went back to the pool
    s.bridge_gym_url = ""
    db_session.commit()
    return sid


def test_finalize_refuses_a_released_world_instead_of_shipping_a_strangers(client, released_attempt):
    r = client.post(f"/api/sessions/{released_attempt}/finalize", json={"versionId": str(uuid4())})
    assert r.status_code in (404, 409), r.text
    if r.status_code == 409:
        assert "not open" in r.json()["detail"], r.text


def test_resetting_a_released_world_is_refused(client, released_attempt):
    """Reset is the most destructive of the lot — it wipes the world outright."""
    r = client.post(f"/api/sessions/{released_attempt}/live/reset-world")
    assert r.status_code == 409, r.text
    assert "not open" in r.json()["detail"], r.text
