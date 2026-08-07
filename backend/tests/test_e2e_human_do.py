"""The whole product, once: do a task by hand and ship the sample.

Everything else tests a seam. This walks the path an annotator actually takes —
raw interactions in the live gym, folded into steps, approved, scored against a
suite, replayed from a clean reset, frozen, exported — and then asks the one
question the platform exists to answer: is the thing that comes out the other end
something a lab could load and train on?

That question had no test at all, which is how the bundle came to ship a cart
naming products it did not contain, screenshots whose bytes were never written,
and a reward computed from something other than the suite beside it.

Only two seams are faked: the gym engine and the browser. Every line of the
recorder, materializer, version graph, finalizer and exporter is the real thing.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app import models
from app.api import versions as vapi
from app.api.export import build_sample

# What the annotator does: search for the socks, open them, add to cart. Enough
# shape to exercise a fill, a keypress and two clicks — the kinds that carry a
# value, a key and a locator respectively.
HAND_RUN = [
    ("mouseDown", {"nx": 0.31, "ny": 0.08}, {"testId": "search-input", "role": "input", "name": "q"}),
    ("mouseUp", {"nx": 0.31, "ny": 0.08, "clicks": 1}, {"testId": "search-input", "role": "input", "name": "q"}),
    ("keyChar", {"text": "s", "value": "s"}, {"testId": "search-input", "role": "input", "name": "q"}),
    ("keyChar", {"text": "o", "value": "so"}, {"testId": "search-input", "role": "input", "name": "q"}),
    ("mouseDown", {"nx": 0.5, "ny": 0.4}, {"testId": "p_socks", "role": "a", "name": "Wool socks"}),
    ("mouseUp", {"nx": 0.5, "ny": 0.4, "clicks": 1}, {"testId": "p_socks", "role": "a", "name": "Wool socks"}),
    ("mouseDown", {"nx": 0.8, "ny": 0.5}, {"testId": "add-to-cart", "role": "button", "name": "Add to cart"}),
    ("mouseUp", {"nx": 0.8, "ny": 0.5, "clicks": 1}, {"testId": "add-to-cart", "role": "button", "name": "Add to cart"}),
]

SEED_WORLD = {
    "task_id": "E2E/socks", "seed": 0, "step": 0,
    "shop": {
        # The CATALOG, not a count of it. A bundle's initial_state has to be a
        # world a client can reset into.
        "products": {"p_socks": {"id": "p_socks", "title": "Wool socks", "price": 22.0, "stock": 5}},
        "cart": {"items": []},
        "orders": {},
    },
}


class FakeBrowser:
    """Answers every replayed action, and remembers what it was asked to do."""

    def __init__(self):
        self.performed: list[tuple] = []
        self.gym = None

    def act(self, kind, locator, args):
        testid = (locator or {}).get("testId")
        self.performed.append((kind, testid, (args or {}).get("value")))
        # Replaying the add-to-cart moves the engine, exactly as the annotator's
        # own click did while they were recording.
        if testid == "add-to-cart" and self.gym:
            self.gym.cart_filled = True
        return {"ok": True, "resolved": {"selector": f'[data-test-id="{testid}"]'}}

    def world(self):
        # The replay executor is asked for the world it ended in.
        return self.gym.world() if self.gym else {}


class FakeGym:
    """A world that gains a cart line when the replay clicks add-to-cart, and a
    verdict in the gym's real shape (`all_milestones`, `fired_at_step`)."""

    def __init__(self, browser):
        self.browser, self.resets = browser, []
        # One flag drives the world, and BOTH paths set it: the test flips it
        # where the annotator's click landed, and `act` flips it when the replay
        # reaches the same step. A fake whose world moves on replay but not
        # during recording just trips the divergence gate, which proves nothing
        # about the pipeline.
        self.cart_filled = False

    def reset(self, task_id, seed):
        self.resets.append((task_id, seed))
        self.cart_filled = False
        return {"ok": True}

    def world(self):
        items = [{"product_id": "p_socks", "quantity": 1}] if self.cart_filled else []
        return {**SEED_WORLD, "shop": {**SEED_WORLD["shop"], "cart": {"items": items}}}

    def verify(self, step=0):
        in_cart = self.cart_filled
        return {
            "score": 1.0 if in_cart else 0.0, "success": in_cart,
            "newly_fired": [], "missed_milestones": [],
            "all_milestones": [
                # Fires on the FIRST verify — the case `or -1` used to read as
                # never-fired, and the reason a correct run scored 0.
                {"name": "socks_in_cart", "weight": 1.0,
                 "fired_at_step": 0 if in_cart else -1, "required": True, "forbidden": False},
                {"name": "no_extra_charge", "weight": 1.0,
                 "fired_at_step": -1, "required": False, "forbidden": True},
            ],
        }


@pytest.fixture()
def wired(monkeypatch):
    browser = FakeBrowser()
    gym = FakeGym(browser)
    browser.gym = gym
    monkeypatch.setattr(vapi.workspace, "endpoint_for", lambda db, sid: gym)
    monkeypatch.setattr(vapi.gym_client, "LiveBrowserClient", lambda **kw: browser)
    monkeypatch.setattr(vapi.live_api, "open_scratch_browser", lambda url, owner: ("live-e2e", "tk"))
    monkeypatch.setattr(vapi.live_api, "close_scratch_browser", lambda sid: None)
    monkeypatch.setattr(vapi.live_api, "browser_visible_gym_url", lambda base: base)
    monkeypatch.setattr(vapi.live_world, "world_for", lambda db, attempt: gym)
    return browser, gym


@pytest.fixture()
def task(db_session):
    t = models.Task(
        external_id=f"E2E/socks_{uuid4().hex[:6]}", title="Buy the socks",
        prompt="Add the wool socks to my cart.", source="gym", seed=0,
        seed_state={"seed": 0, "world_view": "full", "world": SEED_WORLD},
        meta={"primaryApp": "shop", "apps": {"shop": {}}},
    )
    db_session.add(t)
    db_session.commit()
    return t


def _batch(events, first_i, tag):
    return [
        {"kind": k, "payload": {**p, "t": 1000 + (first_i + i) * 120}, "target": tgt,
         "url": "http://localhost:5201/", "tab": "shop", "clientEventId": f"{tag}{first_i + i}"}
        for i, (k, p, tgt) in enumerate(events)
    ]


def _do_the_task(client, sid, gym, tag="e"):
    """Post the interactions the way the pane does — in batches, with the engine
    moving under them. The add-to-cart click is what fills the cart, so the world
    changes between the two."""
    r1 = client.post(f"/api/sessions/{sid}/events", json=_batch(HAND_RUN[:6], 0, tag))
    assert r1.status_code == 200, r1.json()
    gym.cart_filled = True                       # the click the annotator is about to send
    r2 = client.post(f"/api/sessions/{sid}/events", json=_batch(HAND_RUN[6:], 6, tag))
    assert r2.status_code == 200, r2.json()
    return {"recorded": r1.json()["recorded"] + r2.json()["recorded"]}


def test_a_task_done_by_hand_becomes_a_sample_a_lab_could_load(
        client, reviewer_client, db_session, task, wired):
    browser, gym = wired

    # 1. open the task and do it
    r = client.post(f"/api/tasks/{task.external_id}/sessions", json={"fresh": True})
    assert r.status_code == 200, r.json()
    sid = r.json()["sessionId"]
    s = db_session.get(models.ReviewSession, UUID(sid))
    s.mode = "human_do"          # what opening the live pane sets
    db_session.commit()

    out = _do_the_task(client, sid, gym)
    assert out["recorded"] == len(HAND_RUN)

    steps = client.get(f"/api/sessions/{sid}/versions").json()
    head = next(v for v in steps["versions"] if v["isHead"])
    assert head["stepCount"] > 0, "the annotator's own actions ARE the trajectory"

    flat = client.get(f"/api/sessions/{sid}/versions/{head['id']}/steps").json()["steps"]
    kinds = [st["type"] for st in flat]
    assert "click" in kinds and "fill" in kinds, f"got {kinds}"
    rows = db_session.query(models.TrajectoryStep).join(
        models.Trajectory, models.Trajectory.id == models.TrajectoryStep.trajectory_id
    ).filter(models.Trajectory.session_id == UUID(sid)).all()
    assert rows and all(r.semantic_locator for r in rows), (
        "every step needs a locator or finalize refuses the whole attempt"
    )

    # 2. approve the version — the annotator's answer
    rev = next(v for v in client.get(f"/api/sessions/{sid}/versions").json()["versions"]
               if v["id"] == head["id"])["revision"]
    assert client.post(f"/api/sessions/{sid}/versions/{head['id']}/status",
                       json={"status": "approved", "expectedRevision": rev}).status_code == 200

    # 3. the suite, keyed the way the platform actually writes it (m0..mN)
    suite = models.VerifierSuite(session_id=UUID(sid), version=1)
    db_session.add(suite)
    db_session.flush()
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="m0", level="backend",
                                   assertion="the socks are in the cart", code=""))
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="m1", level="safety",
                                   assertion="nothing extra was charged", code=""))
    db_session.commit()

    # 4. ship it
    r = client.post(f"/api/sessions/{sid}/finalize", json={"versionId": head["id"]})
    assert r.status_code == 200, r.json()
    assert r.json()["reward"] == 1, (
        "both milestones held — one fired at step 0, which used to read as never"
    )
    assert gym.resets, "finalize replays from a CLEAN reset, not from a checkpoint"

    # 5. a reviewer accepts it as the golden
    # Adjudication is reviewer-gated: an annotator cannot accept their own work.
    rr = reviewer_client.post(f"/api/qa/tasks/{task.external_id}/adjudicate", json={"sessionId": sid})
    assert rr.status_code == 200, rr.json()

    # ---------------------------------------------------------------- the bundle
    sample = build_sample(db_session, db_session.get(models.ReviewSession, UUID(sid)))

    assert sample["schema"].startswith("golden-sample/"), "every row says what shape it is"
    assert sample["submission"]["accepted"] is True

    # The evaluation triplet's first leg is a world, not a description of one.
    init = sample["initial_state"]
    assert init and (init.get("shop") or {}).get("products"), (
        "initial_state must be a world a client can reset INTO"
    )

    # The trajectory is (what was there) -> (what was done) -> (what changed).
    golden = sample["golden_trajectory"]
    assert golden, "a sample with no trajectory is not a sample"
    for st in golden:
        assert st["actor"] == "human"
        assert "observation" in st and "screenshot" in st
        assert st["locator"], "a step nobody can re-target is not trainable"
    assert any(st["world_delta"] for st in golden), (
        "at least one step has to say what it changed, or there is no signal here"
    )

    # The reward names what produced it, per check.
    assert sample["reward"] == 1
    assert {v["id"] for v in sample["verifiers"]} == {"m0", "m1"}
    assert all(v.get("result") == "pass" for v in sample["verifiers"]), sample["verifiers"]

    # And the seed is the one this was RECORDED at, not the task's current one.
    assert sample["task"]["seed"] == 0


def test_a_task_done_badly_does_not_ship_as_a_golden(client, db_session, task, wired):
    """The other half. A suite that fails must not reach reward 1 because the gym
    was happy, and finalize must refuse rather than shipping a sample its own
    verifiers refute."""
    browser, gym = wired

    sid = client.post(f"/api/tasks/{task.external_id}/sessions",
                      json={"fresh": True}).json()["sessionId"]
    s = db_session.get(models.ReviewSession, UUID(sid))
    s.mode = "human_do"
    db_session.commit()

    # Search and open the product, but never add it to the cart.
    body = [
        {"kind": k, "payload": {**p, "t": 1000 + i * 120}, "target": tgt,
         "url": "http://localhost:5201/", "tab": "shop", "clientEventId": f"b{i}"}
        for i, (k, p, tgt) in enumerate(HAND_RUN[:6])
    ]
    assert client.post(f"/api/sessions/{sid}/events", json=body).status_code == 200

    head = next(v for v in client.get(f"/api/sessions/{sid}/versions").json()["versions"] if v["isHead"])
    rev = head["revision"]
    client.post(f"/api/sessions/{sid}/versions/{head['id']}/status",
                json={"status": "approved", "expectedRevision": rev})

    suite = models.VerifierSuite(session_id=UUID(sid), version=1)
    db_session.add(suite)
    db_session.flush()
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="m0", level="backend",
                                   assertion="the socks are in the cart", code=""))
    db_session.commit()

    r = client.post(f"/api/sessions/{sid}/finalize", json={"versionId": head["id"]})
    assert r.status_code == 409, "an attempt its own suite refutes must not ship"
    assert db_session.query(models.Submission).filter(
        models.Submission.session_id == UUID(sid)).count() == 0
