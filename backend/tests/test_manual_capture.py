"""Manual golden capture over HTTP: explore freely, commit deliberately, and
only what replays.

The API is patched at the two seams that need a real browser (the live executor
and the gym world) — everything else is the production path."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app import models
from app.api import versions as vapi


@pytest.fixture()
def gym_task(db_session):
    task = models.Task(external_id=f"M98_manual/{uuid4().hex[:6]}", title="manual", prompt="do it", source="gym")
    sysann = models.Annotator(email=f"sys-{uuid4().hex[:6]}@system.local")
    db_session.add_all([task, sysann])
    db_session.flush()
    bench = models.ReviewSession(task_id=task.id, annotator_id=sysann.id, source="gym", status="benchmark_run")
    db_session.add(bench)
    db_session.flush()
    traj = models.Trajectory(session_id=bench.id, agent="gpt-5.5", source="gym", raw={"steps": []})
    db_session.add(traj)
    db_session.flush()
    for i in range(3):
        db_session.add(models.TrajectoryStep(
            trajectory_id=traj.id, idx=i, action_type="click", description=f"agent step {i}", actor="agent",
        ))
    db_session.commit()
    return task


class FakeLive:
    """A live browser with a precondition model: `reveals` is what exploration
    would have uncovered, so a commit that skips it genuinely cannot run."""

    def __init__(self, present=("btn-cart",), reveals=None, world=None):
        self.present = set(present)
        self.reveals = reveals or {}
        self._world = world if world is not None else {"cart": [], "step": 0}
        self.performed: list[str] = []

    def act(self, kind, locator, args):
        if kind == "navigate":
            self.performed.append("navigate")
            self._world["step"] += 1
            return {"ok": True, "resolved": {"url": (args or {}).get("url", "")}}
        t = (locator or {}).get("testId") or ""
        if t not in self.present:
            return {"ok": False, "error": "no element matched the locator"}
        self.performed.append(t)
        self._world["step"] += 1
        for r in self.reveals.get(t, []):
            self.present.add(r)
        return {"ok": True, "resolved": {"selector": f'[data-test-id="{t}"]', "url": f"/after/{t}"}}

    def world(self):
        return dict(self._world)


class FakeGym:
    def __init__(self, live):
        self.live = live

    def load_state(self, task_id, seed, state, step=None):
        return {"ok": True}

    def world(self):
        return self.live.world()


@pytest.fixture()
def live(monkeypatch):
    """Bind the API's two browser-dependent seams to a fake."""
    holder = {}

    def install(browser):
        holder["live"] = browser
        monkeypatch.setattr(vapi.workspace, "endpoint_for", lambda db, sid: FakeGym(browser))
        monkeypatch.setattr(vapi.gym_client, "LiveBrowserClient", lambda **kw: browser)
        return browser

    return install


def _session(client, task):
    return client.post(f"/api/tasks/{task.external_id}/sessions", json={}).json()["sessionId"]


def _commit(client, sid, vid, actions, **kw):
    return client.post(f"/api/sessions/{sid}/versions/{vid}/commit", json={
        "actions": actions, "liveSessionId": "live-1", "ticket": "t", **kw,
    })


def _click(test_id, **kw):
    return {"kind": "click", "locator": {"testId": test_id}, **kw}


# --------------------------------------------------------------------------- recording
def test_exploration_is_recorded_but_commits_nothing(client, gym_task, db_session):
    sid = _session(client, gym_task)
    r = client.post(f"/api/sessions/{sid}/events", json=[
        {"kind": "click", "target": {"testId": "btn-menu"}},
        {"kind": "scroll", "payload": {"auto": True, "dy": 200}},
        {"kind": "click", "target": {"testId": "btn-cart"}},
    ])
    assert r.status_code == 200 and r.json()["recorded"] == 3
    assert db_session.query(models.InteractionEvent).count() == 3
    assert db_session.query(models.TrajectoryStep).filter(models.TrajectoryStep.actor == "human").count() == 0


def test_candidate_actions_are_coalesced_and_carry_semantic_locators(client, gym_task):
    sid = _session(client, gym_task)
    client.post(f"/api/sessions/{sid}/events", json=[
        {"kind": "key", "target": {"testId": "input-q"}, "payload": {"value": "m", "t": 0}},
        {"kind": "key", "target": {"testId": "input-q"}, "payload": {"value": "mu", "t": 60}},
        {"kind": "key", "target": {"testId": "input-q"}, "payload": {"value": "mug", "t": 120}},
        {"kind": "scroll", "payload": {"auto": True}},
    ])
    acts = client.get(f"/api/sessions/{sid}/actions").json()["actions"]
    assert [a["kind"] for a in acts] == ["fill"], "12 keystrokes are one action, and an auto-scroll is none"
    assert acts[0]["args"]["value"] == "mug"
    assert acts[0]["locator"] == {"testId": "input-q"}


def test_a_password_field_is_redacted_before_it_is_stored(client, gym_task, db_session):
    sid = _session(client, gym_task)
    client.post(f"/api/sessions/{sid}/events", json=[
        {"kind": "key", "target": {"type": "password", "testId": "pw"}, "payload": {"value": "hunter2"}},
    ])
    ev = db_session.query(models.InteractionEvent).one()
    assert "hunter2" not in str(ev.payload) and ev.payload["redacted"] is True


# --------------------------------------------------------------------------- commit
def test_a_replayable_sequence_becomes_committed_steps(client, gym_task, live, db_session):
    live(FakeLive(present=("btn-cart", "link-checkout")))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()

    r = _commit(client, sid, v1["id"], [_click("btn-cart"), _click("link-checkout")],
                intents=["put the mug in the cart", "go to checkout"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["committed"] == 2

    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    assert [s["actor"] for s in steps][-2:] == ["human", "human"]
    assert steps[-2]["humanIntent"] == "put the mug in the cart", "the human's own 'why' is never synthesized"


def test_the_resolved_target_and_an_end_checkpoint_are_recorded(client, gym_task, live, db_session):
    live(FakeLive(present=("btn-cart",)))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    _commit(client, sid, v1["id"], [_click("btn-cart")])

    st = db_session.query(models.TrajectoryStep).filter(models.TrajectoryStep.actor == "human").one()
    assert st.resolved_target["selector"] == '[data-test-id="btn-cart"]'
    assert st.after_checkpoint_id is not None, "without an end state the next fork has nothing to start from"


def test_a_sequence_depending_on_discarded_exploration_is_rejected(client, gym_task, live, db_session):
    """The annotator opened a menu while exploring and committed only the option
    click. From the branch's clean start the option does not exist."""
    live(FakeLive(present=("btn-menu",), reveals={"btn-menu": ["opt-gift"]}))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()

    r = _commit(client, sid, v1["id"], [_click("opt-gift")])
    assert r.status_code == 422
    assert r.json()["detail"]["at"] == 0
    assert db_session.query(models.TrajectoryStep).filter(models.TrajectoryStep.actor == "human").count() == 0, (
        "a rejected sequence must commit NOTHING — not even its valid prefix"
    )


def test_adding_the_missing_step_makes_the_same_sequence_commit(client, gym_task, live):
    live(FakeLive(present=("btn-menu",), reveals={"btn-menu": ["opt-gift"]}))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    r = _commit(client, sid, v1["id"], [_click("btn-menu"), _click("opt-gift")])
    assert r.status_code == 200 and r.json()["committed"] == 2


def test_a_dry_run_shows_where_it_broke_without_committing(client, gym_task, live, db_session):
    live(FakeLive(present=("btn-cart",)))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()

    r = _commit(client, sid, v1["id"], [_click("btn-cart"), _click("ghost")], dryRun=True)
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["rejectedAt"] == 1 and r.json()["committed"] == 0
    assert db_session.query(models.TrajectoryStep).filter(models.TrajectoryStep.actor == "human").count() == 0


def test_an_empty_commit_is_refused(client, gym_task, live):
    live(FakeLive())
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    assert _commit(client, sid, v1["id"], []).status_code == 422


def test_an_unreachable_live_browser_is_not_reported_as_a_bad_trajectory(client, gym_task, live):
    """Telling an annotator their work is wrong when the service is merely down
    would send them rewriting a correct trajectory."""
    class Down(FakeLive):
        def act(self, kind, locator, args):
            return {"ok": False, "error": "live browser unreachable"}

    live(Down())
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    r = _commit(client, sid, v1["id"], [_click("btn-cart")])
    assert r.status_code == 422 and "unreachable" in r.json()["detail"]["reason"]


# --------------------------------------------------------------------------- hybrid
def test_a_hybrid_branch_reports_agent_prefix_then_human_suffix(client, gym_task, live):
    """The realistic shape: the agent got three steps in, the human rejected one
    and finished by hand. Version-level `kind` cannot express that; per-step
    `actor` must."""
    live(FakeLive(present=("btn-cart",)))
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]

    v2 = client.post(f"/api/sessions/{sid}/versions/fork", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "mode": "before",
    }).json()
    assert _commit(client, sid, v2["id"], [_click("btn-cart")]).status_code == 200

    flat = client.get(f"/api/sessions/{sid}/versions/{v2['id']}/steps").json()["steps"]
    assert [s["actor"] for s in flat] == ["agent", "agent", "human"]
    assert [s["inherited"] for s in flat] == [True, True, False]


def test_committing_to_someone_elses_version_is_not_found(client_for, gym_task, live):
    live(FakeLive(present=("btn-cart",)))
    a, b = client_for("ela@deccan.ai"), client_for("nav@deccan.ai")
    sa = a.post(f"/api/tasks/{gym_task.external_id}/sessions", json={}).json()["sessionId"]
    sb = b.post(f"/api/tasks/{gym_task.external_id}/sessions", json={}).json()["sessionId"]
    vb = b.post(f"/api/sessions/{sb}/versions/baseline").json()
    assert _commit(a, sa, vb["id"], [_click("btn-cart")]).status_code == 404


# --------------------------------------------------------------------------- agent handoff
def _fake_branch(monkeypatch, session_factory, steps, world=None):
    """Run the background branch job INLINE so the test observes its real effects."""
    from app.api import gym as gym_api

    # The job opens its OWN db session in production (it runs on a worker thread);
    # point that at the test database.
    monkeypatch.setattr(vapi, "SessionLocal", session_factory)

    class Gym:
        base_url = "http://fake"

        def resume_run(self, *a, **k):
            return {"trajectory": {"steps": steps}}

        def world(self):
            return world or {"step": 9}

    import contextlib as _c

    @_c.contextmanager
    def ws(_attempt_id):
        yield Gym()

    monkeypatch.setattr(gym_api, "_agent_workspace", ws)

    class InlineJob:
        id = "bg-1"
        status = "done"

    monkeypatch.setattr(vapi.jobs.store, "submit",
                        lambda name, fn, *args: (fn(*args), InlineJob())[1])


def test_a_branch_run_lands_as_a_candidate_the_human_must_choose(client, gym_task, monkeypatch, db_session, _session_factory):
    _fake_branch(monkeypatch, _session_factory, [
        {"action_kind": "click", "description": "check orders first", "url_after": "/orders",
         "world_after": {"step": 4}, "reasoning": "verify transit"},
    ])
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": v1["id"], "expectedRevision": 0})
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]

    r = client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": steps[1]["stepId"],
        "mode": "before", "correction": "check the transit status before cancelling",
    })
    assert r.status_code == 200, r.text
    child_id = r.json()["versionId"]

    listing = client.get(f"/api/sessions/{sid}/versions").json()
    assert listing["headVersionId"] == v1["id"], "the run must not select itself"
    child = next(v for v in listing["versions"] if v["id"] == child_id)
    assert child["status"] == "candidate" and child["stepCount"] == 2  # 1 inherited + 1 produced

    flat = client.get(f"/api/sessions/{sid}/versions/{child_id}/steps").json()["steps"]
    assert flat[-1]["description"] == "check orders first"
    assert flat[-1]["guidance"] == "check the transit status before cancelling", (
        "the reviewer's instruction is provenance on the step it produced"
    )


def test_the_annotator_can_then_select_the_candidate(client, gym_task, monkeypatch, _session_factory):
    _fake_branch(monkeypatch, _session_factory, [{"action_kind": "click", "description": "fixed", "world_after": {"step": 4}}])
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    child = client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": steps[1]["stepId"],
    }).json()["versionId"]

    rev = client.get(f"/api/sessions/{sid}/versions").json()["revision"]
    r = client.post(f"/api/sessions/{sid}/versions/select", json={"versionId": child, "expectedRevision": rev})
    assert r.status_code == 200
    assert client.get(f"/api/sessions/{sid}/versions").json()["headVersionId"] == child


def test_a_completed_run_is_listed_with_its_cap_accounting(client, gym_task, monkeypatch, _session_factory):
    _fake_branch(monkeypatch, _session_factory, [{"action_kind": "click", "description": "x", "world_after": {"step": 1}}])
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": steps[1]["stepId"], "idempotencyKey": "k1",
    })
    runs = client.get(f"/api/sessions/{sid}/runs").json()
    assert runs["agentCallCount"] == 1 and runs["cap"] is None, "the cap ships only after manual fallback"
    assert runs["runs"][0]["status"] == "done" and runs["runs"][0]["countsAgainstCap"] is True


def test_the_cap_refuses_a_further_run_when_configured(client, gym_task, monkeypatch, db_session, _session_factory):
    from app.config import settings

    _fake_branch(monkeypatch, _session_factory, [{"action_kind": "click", "description": "x", "world_after": {"step": 1}}])
    monkeypatch.setattr(settings, "agent_run_cap", 1)
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]

    first = client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": steps[1]["stepId"], "idempotencyKey": "a"})
    assert first.status_code == 200
    second = client.post(f"/api/sessions/{sid}/versions/agent-run", json={
        "parentVersionId": v1["id"], "stepId": steps[2]["stepId"], "idempotencyKey": "b"})
    assert second.status_code == 429 and "manually" in second.json()["detail"]


def test_the_same_idempotency_key_returns_the_same_run(client, gym_task, monkeypatch, _session_factory):
    _fake_branch(monkeypatch, _session_factory, [{"action_kind": "click", "description": "x", "world_after": {"step": 1}}])
    sid = _session(client, gym_task)
    v1 = client.post(f"/api/sessions/{sid}/versions/baseline").json()
    steps = client.get(f"/api/sessions/{sid}/versions/{v1['id']}/steps").json()["steps"]
    body = {"parentVersionId": v1["id"], "stepId": steps[1]["stepId"], "idempotencyKey": "same"}

    a = client.post(f"/api/sessions/{sid}/versions/agent-run", json=body).json()
    b = client.post(f"/api/sessions/{sid}/versions/agent-run", json=body).json()
    assert a["versionId"] == b["versionId"] and b.get("replayed") is True
    assert client.get(f"/api/sessions/{sid}/runs").json()["agentCallCount"] == 1
