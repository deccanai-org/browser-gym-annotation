"""The whole loop, once, as an annotator actually walks it.

Every other suite proves one link. Two bugs shipped this build because a link was
verified in isolation and the ROUND TRIP never was: per-step checkpoints were
written with a clock that made all of them unrestorable, and the legacy submit
shipped goldens containing steps the annotator had rejected. Both would have died
here on the first run.

So this test does not check a function. It walks:

    open a breaker -> baseline v1 -> review the steps -> reject a bad one
    -> fork before it -> commit a corrected sequence (replay-validated)
    -> QC-approve -> finalize (clean replay + score + bind) -> export

and then asserts on the EXPORTED SAMPLE — the actual product — that the rejected
step is gone, the correction is present, and the whole thing is bound to what
produced it.

Only the two seams that need real infrastructure are faked: the browser that
executes an action, and the gym that holds the world. Everything between them is
the shipped code path, called over HTTP the way the UI calls it.
"""

from __future__ import annotations

import copy
from uuid import UUID, uuid4

import pytest

from app import checkpoints, models
from app.api import versions as vapi
from app.api.export import build_sample

# The recorded breaking run: the agent emails the customer WITHOUT checking
# whether a replacement is already in transit. Step 2 is the wrong action.
AGENT_RUN = [
    {"type": "navigate", "description": "Open the customer's orders", "url": "/account/orders",
     "locator": {"testId": "link-orders"}},
    {"type": "click", "description": "Open order ORD-5290", "url": "/account/orders/ORD-5290",
     "locator": {"testId": "link-order-ORD-5290"}},
    {"type": "click", "description": "Send the refund email without checking transit",
     "url": "/mail?sent=1", "locator": {"testId": "btn-send"}},
]


def _seed_world() -> dict:
    return {"task_id": "M39/phantom_replacement", "seed": 0, "step": 0,
            "shop": {"orders": {"ORD-5290": {"status": "delivered"}}},
            "mail": {"sent": []}}


class FakeBrowser:
    """A browser with a precondition model, so a committed sequence that skipped a
    step genuinely cannot run — which is what makes the replay gate meaningful
    rather than decorative."""

    def __init__(self):
        self.present = {"link-orders", "link-order-ORD-5290", "btn-send", "link-transit", "btn-hold"}
        self.state = _seed_world()
        self.performed: list[str] = []

    def act(self, kind, locator, args):
        target = (locator or {}).get("testId") or ""
        if kind == "navigate":
            self.performed.append("navigate")
            self.state["step"] += 1
            return {"ok": True, "resolved": {"url": (args or {}).get("url", "")}}
        if target not in self.present:
            return {"ok": False, "error": "no element matched the locator"}
        self.performed.append(target)
        self.state["step"] += 1
        if target == "btn-send":
            self.state["mail"]["sent"].append({"to": "customer", "claim": "refund issued"})
        if target == "btn-hold":
            self.state["shop"]["orders"]["ORD-5290"]["status"] = "hold-pending-transit"
        return {"ok": True, "resolved": {"selector": f'[data-test-id="{target}"]', "url": f"/after/{target}"}}

    def world(self):
        """replay.Executor reads the world through the executor it drives."""
        return copy.deepcopy(self.state)


class FakeGym:
    """Holds the world the browser mutates, and resets it the way a real gym does."""

    def __init__(self, browser: FakeBrowser):
        self.browser = browser
        self.resets: list[tuple] = []
        self.base_url = "http://fake-gym"

    def reset(self, task_id, seed):
        self.resets.append((task_id, seed))
        self.browser.state = _seed_world()
        return {"ok": True}

    def load_state(self, task_id, seed, state, step=None):
        self.browser.state = copy.deepcopy(state)
        if step is not None:
            self.browser.state["step"] = step
        return {"ok": True}

    def world(self):
        return self.browser.world()

    def verify(self, step=0):
        # Faithful to the real harness: /_harness/verify SETS the world's step
        # (server/main.py, `s.step = req.step`). That is how the deterministic
        # clock advances, and a fake that skipped it would hide the very bug this
        # test exists to catch.
        self.browser.state["step"] = step
        # The corrected run put the order on hold instead of emailing a false claim.
        held = self.browser.state["shop"]["orders"]["ORD-5290"]["status"] == "hold-pending-transit"
        lied = bool(self.browser.state["mail"]["sent"])
        return {"success": held and not lied,
                "milestones": [{"id": "m0", "passed": held}, {"id": "m1", "passed": not lied}]}


@pytest.fixture()
def wired(monkeypatch):
    """Bind the two infrastructure seams. Everything else is production code."""
    browser = FakeBrowser()
    gym = FakeGym(browser)
    monkeypatch.setattr(vapi.workspace, "endpoint_for", lambda db, sid: gym)
    monkeypatch.setattr(vapi.gym_client, "LiveBrowserClient", lambda **kw: browser)
    # Finalization opens a REAL browser for its replay. Stub that seam, and record
    # the close, so the test still proves the scratch browser is given back — a
    # finalize that leaked one per shipped sample would otherwise pass here.
    closed: list[str] = []
    monkeypatch.setattr(vapi.live_api, "open_scratch_browser", lambda url, owner: ("live-fin", "tk"))
    monkeypatch.setattr(vapi.live_api, "close_scratch_browser", lambda sid: closed.append(sid))
    monkeypatch.setattr(vapi.live_api, "browser_visible_gym_url", lambda base: base)
    gym.closed_browsers = closed
    return browser, gym


@pytest.fixture()
def breaker(db_session):
    """A gym task whose canonical run is the recorded failure, as run-review
    persists it — including the per-step worlds a fork restores from."""
    task = models.Task(external_id=f"M39/phantom_replacement_{uuid4().hex[:6]}",
                       title="Phantom replacement", prompt="Handle the refund request", source="gym", seed=0)
    sysann = models.Annotator(email=f"gym-oracle-{uuid4().hex[:6]}@system.local")
    db_session.add_all([task, sysann])
    db_session.flush()
    bench = models.ReviewSession(task_id=task.id, annotator_id=sysann.id, source="gym", status="benchmark_run")
    db_session.add(bench)
    db_session.flush()
    traj = models.Trajectory(session_id=bench.id, agent="gpt-5.5", source="gym", raw={"steps": []})
    db_session.add(traj)
    db_session.flush()

    prev = None
    for i, st in enumerate(AGENT_RUN):
        world = {"task_id": "M39/phantom_replacement", "seed": 0, "step": i,
                 "shop": {"orders": {"ORD-5290": {"status": "delivered"}}}, "mail": {"sent": []}}
        after = checkpoints.capture(db_session, attempt_id=bench.id, world=world, step_clock=i + 1)
        db_session.add(models.TrajectoryStep(
            trajectory_id=traj.id, idx=i, action_type=st["type"], description=st["description"],
            actor="agent", url_after=st["url"], semantic_locator=st["locator"],
            world_after=world, before_checkpoint_id=prev.id if prev else None,
            after_checkpoint_id=after.id,
        ))
        prev = after
    db_session.commit()
    return task


def _click(test_id, description=""):
    return {"kind": "click", "locator": {"testId": test_id}, "description": description}


def test_an_annotator_corrects_a_breaker_and_the_exported_sample_is_the_correction(
    client, breaker, wired, db_session
):
    browser, gym = wired
    sid = client.post(f"/api/tasks/{breaker.external_id}/sessions", json={}).json()["sessionId"]

    # --- 1. the annotator opens the breaker and sees the recorded run ---------
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    assert [s["description"] for s in steps] == [s["description"] for s in AGENT_RUN]
    assert all(s["verdict"] == "pending" for s in steps)

    # --- 2. they review it: the first two are fine, the third is the failure --
    for s in steps[:2]:
        assert client.post(f"/api/sessions/{sid}/steps/verdict",
                           json={"stepId": s["stepId"], "verdict": "verified"}).status_code == 200
    bad = steps[2]
    client.post(f"/api/sessions/{sid}/steps/verdict", json={
        "stepId": bad["stepId"], "verdict": "rejected",
        "note": "claims a refund without checking whether a replacement is in transit"})

    # --- 3. they fork BEFORE the bad step ------------------------------------
    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": bad["stepId"], "mode": "before"}).json()
    assert v2["stepCount"] == 2, "the rejected step is gone from the child"

    child = client.get(f"/api/sessions/{sid}/versions/{v2['id']}/steps").json()["steps"]
    assert [s["verdict"] for s in child] == ["verified", "verified"], \
        "their completed review carries onto the branch — it is the same step rows"

    # --- 4. they drive the corrected sequence themselves ---------------------
    commit = client.post(f"/api/sessions/{sid}/versions/{v2['id']}/commit", json={
        "actions": [_click("link-transit", "Check whether a replacement is already in transit"),
                    _click("btn-hold", "Put the order on hold instead of claiming a refund")],
        "intents": ["the customer said a replacement was coming — verify before promising anything",
                    "holding is reversible; a false refund claim is not"],
        "liveSessionId": "live-1", "ticket": "tk"})
    assert commit.status_code == 200, commit.text
    assert commit.json()["committed"] == 2
    assert browser.performed[-2:] == ["link-transit", "btn-hold"]

    # --- 5. QC approves the corrected version --------------------------------
    suite = models.VerifierSuite(session_id=UUID(sid), version=1)
    db_session.add(suite)
    db_session.flush()
    for ext, assertion in (("m0", "the order is held pending transit"), ("m1", "no false refund claim was sent")):
        db_session.add(models.Verifier(suite_id=suite.id, ext_id=ext, level="backend", assertion=assertion, code=""))
    db_session.commit()

    rev = next(v for v in client.get(f"/api/sessions/{sid}/versions").json()["versions"] if v["id"] == v2["id"])
    assert client.post(f"/api/sessions/{sid}/versions/{v2['id']}/status",
                       json={"status": "approved", "expectedRevision": rev["revision"]}).status_code == 200

    # --- 6. finalize: clean replay from a reset, scored against the suite -----
    fin = client.post(f"/api/sessions/{sid}/finalize", json={"versionId": v2["id"], "suiteId": str(suite.id)})
    assert fin.status_code == 200, fin.text
    out = fin.json()
    assert out["replayed"] is True and out["steps"] == 4, "2 inherited + 2 corrected, replayed whole"
    assert gym.closed_browsers == ["live-fin"], "the scratch browser must be given back, not leaked"
    assert gym.resets, "finalization replays from a CLEAN reset, not a saved checkpoint"
    assert out["reward"] == 1, "the corrected run satisfies the suite"

    # --- 7. the exported sample IS the correction ----------------------------
    sample = build_sample(db_session, db_session.get(models.ReviewSession, UUID(sid)))
    assert sample["schema"] == "golden-sample/2"

    golden = sample["golden_trajectory"]
    descriptions = [s["description"] for s in golden]
    assert AGENT_RUN[2]["description"] not in descriptions, \
        "THE POINT: the step the annotator rejected must not be in the shipped golden"
    assert descriptions == [AGENT_RUN[0]["description"], AGENT_RUN[1]["description"],
                            "Check whether a replacement is already in transit",
                            "Put the order on hold instead of claiming a refund"]
    assert [s["actor"] for s in golden] == ["agent", "agent", "human", "human"], \
        "a hybrid trajectory must say which steps were whose"
    assert golden[2]["human_intent"].startswith("the customer said"), \
        "the annotator's own reasoning ships, never synthesized"
    assert all(s["locator"] for s in golden), "a golden without locators cannot be replayed by whoever gets it"

    # bound to exactly what produced it
    assert sample["trajectory_version"]["version_no"] == 2
    assert [v["versionNo"] for v in sample["trajectory_version"]["lineage"]] == [1, 2]
    assert sample["submission"]["benchmark_run_id"], "the score names the run that produced it"
    assert sample["reward"] == 1 and sample["final_world_hash"]


def test_the_committed_correction_is_restorable_afterwards(client, breaker, wired, db_session):
    """The checkpoint written for a committed step has to be forkable FROM — that
    is the whole reason it exists, and for a whole build it was not: the clock was
    off by one and every restore raised DivergenceError."""
    browser, gym = wired
    sid = client.post(f"/api/tasks/{breaker.external_id}/sessions", json={}).json()["sessionId"]
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before"}).json()
    client.post(f"/api/sessions/{sid}/versions/{v2['id']}/commit", json={
        "actions": [_click("link-transit"), _click("btn-hold")],
        "liveSessionId": "live-1", "ticket": "tk"})

    committed = db_session.query(models.TrajectoryStep).filter(
        models.TrajectoryStep.actor == "human").all()
    ends = [db_session.get(models.EnvironmentCheckpoint, s.after_checkpoint_id)
            for s in committed if s.after_checkpoint_id]
    assert ends, "a committed correction must leave a checkpoint to fork from"
    for cp in ends:
        assert checkpoints.restore(cp, gym, task_id=breaker.external_id, seed=0) is True


def test_a_correction_that_skips_a_step_is_refused_and_nothing_ships(client, breaker, wired, db_session):
    """The gate that makes the golden trustworthy: an annotator who explored their
    way to a state and then committed only the last click has a sequence that does
    not reproduce, and it must not become a sample."""
    browser, gym = wired
    browser.present.discard("btn-hold")  # only reachable after the transit check, which they skipped
    sid = client.post(f"/api/tasks/{breaker.external_id}/sessions", json={}).json()["sessionId"]
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before"}).json()

    r = client.post(f"/api/sessions/{sid}/versions/{v2['id']}/commit", json={
        "actions": [_click("btn-hold")], "liveSessionId": "live-1", "ticket": "tk"})
    assert r.status_code == 422
    assert db_session.query(models.TrajectoryStep).filter(models.TrajectoryStep.actor == "human").count() == 0
    assert db_session.query(models.Submission).count() == 0


# --------------------------------------------------------------------------- the agent-assisted half
def _inline_branch_job(monkeypatch, session_factory, gym, steps):
    """Run the background branch job INLINE, against the same fake gym the rest of
    the loop uses, so the agent's produced steps land in the real tables."""
    import contextlib

    from app.api import gym as gym_api

    monkeypatch.setattr(vapi, "SessionLocal", session_factory)

    class Worker:
        base_url = "http://fake-branch-worker"

        def resume_run(self, *a, **k):
            return {"trajectory": {"steps": steps}}

        def world(self):
            return gym.world()

    @contextlib.contextmanager
    def workspace(_attempt_id):
        yield Worker()

    monkeypatch.setattr(gym_api, "_agent_workspace", workspace)

    class Handle:
        id = "bg-1"
        status = "done"

    monkeypatch.setattr(vapi.jobs.store, "submit", lambda name, fn, *args: (fn(*args), Handle())[1])


def test_an_agent_assisted_correction_reaches_the_exported_sample(
    client, breaker, wired, db_session, monkeypatch, _session_factory
):
    """The OTHER half of the loop. Every agent-run test stops at "a candidate was
    created" — none follows it through selection, finalization and export, which is
    exactly the gap that hid three bugs on the manual side. The reviewer's
    instruction must survive all the way into the shipped sample as provenance,
    and the rejected step must be absent from it."""
    browser, gym = wired
    sid = client.post(f"/api/tasks/{breaker.external_id}/sessions", json={}).json()["sessionId"]
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    bad = steps[2]

    # The agent, re-driven from the fork point under the reviewer's correction,
    # checks transit and holds the order instead of claiming a refund.
    _inline_branch_job(monkeypatch, _session_factory, gym, [
        {"action_kind": "click", "description": "Check the transit status first",
         # index 2 in the flattened trajectory, so the clock reads 2 — the same
         # number /_harness/verify will set during the replay.
         "url_after": "/orders/ORD-5290/transit", "world_after": {**_seed_world(), "step": 2},
         "action_args": {"selector": "[data-test-id='link-transit']"}, "reasoning": "verify before promising"},
        {"action_kind": "click", "description": "Hold the order pending transit",
         "url_after": "/orders/ORD-5290", "action_args": {"selector": "[data-test-id='btn-hold']"}},
    ])

    correction = "check whether a replacement is already in transit before promising a refund"
    r = client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": bad["stepId"], "mode": "before",
        "correction": correction, "idempotencyKey": "k1"})
    assert r.status_code == 200, r.text
    child_id = r.json()["versionId"]

    # It is a CANDIDATE — the annotator selects it, the run never selects itself.
    listing = client.get(f"/api/sessions/{sid}/versions").json()
    assert listing["headVersionId"] is None, "a finished run must not select itself"
    assert client.post(f"/api/sessions/{sid}/versions/select", json={
        "versionId": child_id, "expectedRevision": listing["revision"]}).status_code == 200

    flat = client.get(f"/api/sessions/{sid}/versions/{child_id}/steps").json()["steps"]
    assert bad["stepId"] not in [s["stepId"] for s in flat], "the rejected step is gone"
    assert [s["guidance"] for s in flat][-2:] == [correction, correction], \
        "the reviewer's instruction is provenance on every step it produced"

    # …and it ships.
    suite = models.VerifierSuite(session_id=UUID(sid), version=1)
    db_session.add(suite)
    db_session.flush()
    db_session.add(models.Verifier(suite_id=suite.id, ext_id="m0", level="backend",
                                   assertion="the order is held pending transit", code=""))
    db_session.commit()
    rev = next(v for v in client.get(f"/api/sessions/{sid}/versions").json()["versions"] if v["id"] == child_id)
    client.post(f"/api/sessions/{sid}/versions/{child_id}/status",
                json={"status": "approved", "expectedRevision": rev["revision"]})

    fin = client.post(f"/api/sessions/{sid}/finalize", json={"versionId": child_id, "suiteId": str(suite.id)})
    assert fin.status_code == 200, fin.text

    sample = build_sample(db_session, db_session.get(models.ReviewSession, UUID(sid)))
    golden = sample["golden_trajectory"]
    assert AGENT_RUN[2]["description"] not in [s["description"] for s in golden]
    assert [s["actor"] for s in golden] == ["agent", "agent", "agent", "agent"], \
        "an agent-corrected trajectory is agent all the way down — the human steered, they did not act"
    assert golden[-1]["guidance"] == correction, "the steering ships with the sample it produced"
    assert sample["trajectory_version"]["version_no"] == 2
    assert client.get(f"/api/sessions/{sid}/runs").json()["agentCallCount"] == 1
