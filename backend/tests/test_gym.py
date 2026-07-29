"""The live-gym bridge (M6c) degrades cleanly when the gym isn't running."""

from app import gym_client

_DOWN = "http://127.0.0.1:59999"  # nothing listens here


def test_client_returns_none_when_gym_unreachable(monkeypatch):
    monkeypatch.setattr("app.gym_client.settings.gym_url", _DOWN)
    assert gym_client.available() is False
    assert gym_client.verify(0) is None
    assert gym_client.tasks() is None
    assert gym_client.reset("A1/x") is None


def test_status_reports_disconnected(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.settings.gym_url", _DOWN)
    r = client.get("/api/gym/status")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_verify_502_when_gym_down(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.settings.gym_url", _DOWN)
    assert client.post("/api/gym/verify", json={"step": 0}).status_code == 502
    assert client.get("/api/gym/tasks").status_code == 502


def test_run_502_when_gym_down(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.settings.gym_url", _DOWN)
    assert client.post("/api/gym/run", json={"taskId": "A1/x"}).status_code == 502


# ---- async run-review job queue ----


def _poll(client, jid, tries=80):
    import time

    jr = {"status": "queued"}
    for _ in range(tries):
        jr = client.get(f"/api/gym/jobs/{jid}").json()
        if jr["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    return jr


def test_run_review_is_async_and_pollable(client, monkeypatch):
    # gym returns nothing → the worker fails → job reaches a terminal 'error'.
    monkeypatch.setattr("app.gym_client.run_agent", lambda *a, **k: None)
    r = client.post("/api/gym/tasks/A1/x/run-review", json={"agent": "oracle", "seed": 0})
    assert r.status_code == 200
    assert r.json().get("jobId")  # returns a jobId immediately (run is off the request path)
    jr = _poll(client, r.json()["jobId"])
    assert jr["status"] == "error"
    assert client.get("/api/gym/jobs/does-not-exist").status_code == 404


def test_apply_edits_dot_path():
    from app.api.gym import _apply_edits

    base = {"shop": {"orders": {"o1": {"payment_id": "amex"}}}}
    out = _apply_edits(base, {"shop.orders.o1.payment_id": "personal", "shop.new": 5})
    assert out["shop"]["orders"]["o1"]["payment_id"] == "personal"
    assert out["shop"]["new"] == 5
    assert base["shop"]["orders"]["o1"]["payment_id"] == "amex"  # original untouched (deep-copied)


def test_resume_returns_real_verdict(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.resume_verify", lambda *a, **k: {"score": 1.0, "success": True})
    r = client.post("/api/gym/resume", json={"taskId": "A1/x", "seed": 0, "worldState": {"shop": {}}, "urlTrail": ["/"]})
    assert r.status_code == 200
    assert r.json()["reward"] == 1 and r.json()["success"] is True


def test_resume_502_when_gym_unreachable(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.resume_verify", lambda *a, **k: None)
    assert client.post("/api/gym/resume", json={"taskId": "A1/x", "seed": 0}).status_code == 502


def test_resume_surfaces_gym_bad_state_as_422_not_502(client, monkeypatch):
    """A precise gym 4xx (e.g. 422 bad-state overlay) must be surfaced, not
    collapsed into a misleading 502 'gym unreachable'."""
    from app import gym_client

    def _raise(*a, **k):
        raise gym_client.GymBadRequest(422, "could not load state: bad overlay")

    monkeypatch.setattr("app.gym_client.resume_verify", _raise)
    r = client.post("/api/gym/resume", json={"taskId": "M66/x", "seed": 0, "worldState": {"shop": {}}, "urlTrail": ["/"]})
    assert r.status_code == 422
    assert "gym" in r.json()["detail"].lower()


def test_breaker_gate_rejects_non_discriminating_policy(monkeypatch):
    """The gate must KEEP a policy whose violating counterfactual is flagged, and
    REJECT one whose counterfactual is not (it doesn't actually discriminate)."""
    from app.api import gym as gymmod

    monkeypatch.setattr("app.agent.generate_trace_policies", lambda brief, actions: ["A", "B"])
    monkeypatch.setattr("app.agent.generate_policy_counterfactual", lambda pol, actions: f"__CF__ {pol}")

    def fake_judge(policy, trace):
        is_counterfactual = any("__CF__" in (s.get("description") or "") for s in trace)
        if not is_counterfactual:
            return True  # oracle OBEYS both policies
        return False if policy == "A" else True  # A's counterfactual VIOLATES; B's obeys ⇒ B doesn't discriminate

    monkeypatch.setattr("app.agent.judge_trajectory", fake_judge)

    gated = gymmod._gate_policies("brief", [{"idx": 0, "description": "did the task"}], [{"action": "click"}])
    kept = [p for p in gated if p["discriminates"]]
    assert {p["assertion"] for p in kept} == {"A"}  # B was REJECTED by the breaker gate
    assert next(p for p in gated if p["assertion"] == "B")["discriminates"] is False


def test_autogen_verifiers_is_async(client, monkeypatch):
    # gym unreachable → reward-agent job reaches a terminal error, pollable.
    monkeypatch.setattr("app.gym_client.reset", lambda *a, **k: None)
    r = client.post("/api/gym/autogen-verifiers", json={"taskId": "A1/x", "seed": 0, "iterations": 2})
    assert r.status_code == 200 and r.json().get("jobId")
    assert _poll(client, r.json()["jobId"])["status"] == "error"


def test_resume_run_is_async_and_pollable(client, monkeypatch):
    monkeypatch.setattr("app.gym_client.resume_run", lambda *a, **k: None)  # drive fails → terminal error
    r = client.post("/api/gym/resume-run", json={"taskId": "A1/x", "seed": 0, "worldState": {"shop": {}}, "resumeUrl": "/"})
    assert r.status_code == 200 and r.json().get("jobId")
    assert _poll(client, r.json()["jobId"])["status"] == "error"


def test_async_unknown_task_reports_clean_not_found_no_route_leak(client, monkeypatch):
    """D1: a gym 404 inside the async job must surface as a clean 'gym task not
    found' — not a generic 'internal error' that leaks the internal /_harness route."""
    def _raise(*a, **k):
        raise gym_client.GymTaskNotFound("/_harness/run_agent")

    monkeypatch.setattr("app.gym_client.run_agent", _raise)
    r = client.post("/api/gym/tasks/A1/nope/run-review", json={"agent": "oracle", "seed": 0})
    assert r.status_code == 200
    jr = _poll(client, r.json()["jobId"])
    assert jr["status"] == "error"
    assert jr["error"] == "gym task not found"
    assert "_harness" not in jr["error"]  # internal route no longer leaked


def test_autogen_no_suite_message_is_honest_when_key_is_set(client, monkeypatch):
    """D2: when a key IS configured but the reward agent yields no suite, the
    diagnostic must not blame a missing key."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "sk-present")
    monkeypatch.setattr("app.gym_client.reset", lambda *a, **k: {"start_path": "/"})
    monkeypatch.setattr("app.gym_client.world", lambda *a, **k: {"shop": {}})
    monkeypatch.setattr(
        "app.gym_client.run_agent",
        lambda *a, **k: {"trajectory": {"steps": [{"step_idx": 0, "action_kind": "click", "action_args": {}, "active_tab": "shop", "reasoning": "x"}], "task_brief": "b"}},
    )
    monkeypatch.setattr("app.agent.generate_verifier_suite", lambda *a, **k: None)
    monkeypatch.setattr("app.agent.generate_trace_policies", lambda *a, **k: [])
    r = client.post("/api/gym/autogen-verifiers", json={"taskId": "A1/x", "seed": 0, "iterations": 1})
    jr = _poll(client, r.json()["jobId"])
    assert jr["status"] == "done"
    err = jr["review"]["history"][0]["error"]
    assert "needs API key" not in err          # the old misleading blame is gone
    assert "no ANTHROPIC_API_KEY" not in err    # a key IS set → not the no-key branch
    assert "rate-limited or unavailable" in err


def test_job_store_runs_and_captures_outcomes():
    import time

    from app.jobs import JobFailure, JobStore

    st = JobStore()
    ok = st.submit("t", lambda x: x + 1, 41)
    for _ in range(80):
        if st.get(ok.id).status == "done":
            break
        time.sleep(0.02)
    assert st.get(ok.id).status == "done" and st.get(ok.id).result == 42

    def _boom():
        raise JobFailure("boom")

    bad = st.submit("t", _boom)
    for _ in range(80):
        if st.get(bad.id).status == "error":
            break
        time.sleep(0.02)
    assert st.get(bad.id).status == "error" and st.get(bad.id).error == "boom"


# ---- gym-persistence cluster (#1 gym-aware benchmark, #3 stable replay, #4
# submittable) — these read only the DB, so no live gym is needed. -------------


def _synthetic_gym_review(task_id: str, *, success: bool):
    """A minimal (run, review) pair shaped like _run_review_job produces, so
    _persist_gym_review writes a full record (incl. trajectory.raw)."""
    reward = 1 if success else 0
    steps = [
        {"idx": i, "type": "click", "description": f"step {i}", "tabId": "shop",
         "image": f"/api/gym/screenshot?path=s{i}.png", "url": "/shop"}
        for i in range(3)
    ]
    verifiers = [
        {"id": "m0", "level": "ui", "assertion": "a0", "code": "c0", "gymResult": "pass"},
        {"id": "m1", "level": "backend", "assertion": "a1", "code": "c1",
         "gymResult": "pass" if success else "fail"},
    ]
    run = {"seed": 0, "trajectory": {
        "verifier_result": {"score": float(reward), "success": success},
        # Faithful to the real harness: a click records the selector it used
        # (trajectories/openai/*.jsonl — action_args {"selector": "[data-test-id=…]"}).
        # A fixture with empty action_args cannot catch a missing locator, which is
        # exactly how the unreplayable-canonical-run bug survived the suite.
        "steps": [{"reasoning": f"r{i}",
                   "action_kind": "click",
                   "action_args": {"selector": f"[data-test-id='btn-{i}']"}}
                  for i in range(3)],
        "task_category": "M", "task_difficulty": "medium", "initial_url": "/shop"}}
    review = {
        "task": {"title": "T", "prompt": "do it", "priority": "High",
                 "constraints": [], "allowedSites": [], "runSummary": []},
        "steps": steps, "verifiers": verifiers, "gymReward": reward,
        "backendState": {"orders": [], "cart": {"items": []}},
        "gymResume": {"worldState": {"shop": {}}}, "source": "gym", "tabs": [],
    }
    return run, review


def test_persisted_review_404_then_replays_same_run(client, db_session):
    """#3 — before any run the endpoint 404s; after a run it REPLAYS the exact
    persisted payload (steps + screenshots + verdict), so reopening is stable."""
    from app.api.gym import _persist_gym_review

    task_id = "M99/stable_replay"
    assert client.get(f"/api/gym/tasks/{task_id}/persisted-review").status_code == 404

    run, review = _synthetic_gym_review(task_id, success=True)
    _persist_gym_review(db_session, task_id, "oracle", run, review)

    r = client.get(f"/api/gym/tasks/{task_id}/persisted-review")
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    assert len(body["steps"]) == 3
    assert all(s["image"] for s in body["steps"])  # screenshots survive
    assert body["gymReward"] == 1
    # a second read is identical (no re-run, no drift)
    assert client.get(f"/api/gym/tasks/{task_id}/persisted-review").json()["steps"] == body["steps"]


def test_gym_benchmark_scores_from_verdict_and_is_submittable(client, db_session):
    """#1/#4 — a human gym session (no fixture) is scored from the authoritative
    gym verdict (not the empty fixture → not 0), reaches benchmark_run, and
    becomes submittable."""
    from app.api.gym import _persist_gym_review

    task_id = "M98/submit_gym"
    run, review = _synthetic_gym_review(task_id, success=True)
    _persist_gym_review(db_session, task_id, "oracle", run, review)

    snap = client.post(f"/api/tasks/{task_id}/sessions", json={"fresh": True}).json()
    sid = snap["sessionId"]
    verifiers = [{"id": v["id"], "level": v["level"], "assertion": v["assertion"],
                  "code": v["code"], "check": None, "failsUntilCorrected": False,
                  "placeholder": False, "addedByHuman": False, "gymResult": v["gymResult"]}
                 for v in review["verifiers"]]
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": verifiers}).status_code == 200

    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})

    bench = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": [], "overrides": []})
    assert bench.status_code == 200
    out = bench.json()
    assert out["reward"] == 1  # #1: from the gym verdict, NOT 0 from the empty fixture
    assert out["results"] == {"m0": "pass", "m1": "pass"}
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "benchmark_run"  # #4

    sub = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False, "kind": "golden"})
    assert sub.status_code == 200  # #4: gym samples submit
    assert sub.json()["submission"]["kind"] == "golden"


def test_gym_breaker_benchmark_scores_zero_not_from_empty_fixture(client, db_session):
    """A failed gym run scores 0 from the verdict — and 0 is a real breaker
    (needs an override to submit), not an artifact of the empty fixture."""
    from app.api.gym import _persist_gym_review

    task_id = "M97/breaker_gym"
    run, review = _synthetic_gym_review(task_id, success=False)
    _persist_gym_review(db_session, task_id, "oracle", run, review)

    sid = client.post(f"/api/tasks/{task_id}/sessions", json={"fresh": True}).json()["sessionId"]
    verifiers = [{"id": v["id"], "level": v["level"], "assertion": v["assertion"],
                  "code": v["code"], "check": None, "failsUntilCorrected": False,
                  "placeholder": False, "addedByHuman": False, "gymResult": v["gymResult"]}
                 for v in review["verifiers"]]
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": verifiers})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    out = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": [], "overrides": []}).json()
    assert out["reward"] == 0 and out["results"] == {"m0": "pass", "m1": "fail"}
    # reward 0 without an override is rejected at submit
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 0, "override": False}).status_code == 409


def test_drive_forward_does_not_lose_or_shadow_the_original_breaker(client, db_session):
    """The annotator's KEY guarantee: correcting a step + driving forward persists
    the corrected run as a real, scorable record — but the ORIGINAL full breaking
    run is never overwritten and is still what persisted-review reopens. Nothing
    is lost; the correction is additive."""
    from app.api.gym import _persist_gym_review
    from app import models
    from sqlalchemy import select

    task_id = "M95/preserve_original"
    orig_run, orig_review = _synthetic_gym_review(task_id, success=False)  # the breaking run
    orig_review["steps"] = orig_review["steps"]  # 3 steps
    sid_orig = _persist_gym_review(db_session, task_id, "openai", orig_run, orig_review)

    # persisted-review returns the ORIGINAL breaking run
    before = client.get(f"/api/gym/tasks/{task_id}/persisted-review").json()
    assert len(before["steps"]) == 3 and before["gymReward"] == 0

    # annotator drives forward from a corrected step → a CONTINUATION run persisted
    # WITHOUT a replay payload (persist_raw=False), like _resume_run_job does.
    cont_run, cont_review = _synthetic_gym_review(task_id, success=True)  # the corrected continuation
    cont_review["steps"] = cont_review["steps"][:2]  # a shorter continuation
    sid_cont = _persist_gym_review(db_session, task_id, "openai", cont_run, cont_review, persist_raw=False)
    assert sid_cont != sid_orig  # a distinct, additive record

    # persisted-review STILL returns the original breaking run (not the continuation)
    after = client.get(f"/api/gym/tasks/{task_id}/persisted-review").json()
    assert len(after["steps"]) == 3 and after["gymReward"] == 0, "the original breaking run must survive + stay the base"
    assert after["steps"] == before["steps"]

    # BOTH runs are preserved in the DB (nothing deleted/overwritten)
    task = db_session.scalar(select(models.Task).where(models.Task.external_id == task_id))
    trajs = db_session.scalars(
        select(models.Trajectory).join(models.ReviewSession, models.Trajectory.session_id == models.ReviewSession.id)
        .where(models.ReviewSession.task_id == task.id)).all()
    assert len(trajs) == 2, "both the original and the driven-forward run are retained"
    assert sum(1 for t in trajs if t.raw is not None) == 1  # only the original is replayable
    assert sum(1 for t in trajs if t.raw is None) == 1       # the continuation is an audit/verdict record


def test_prompt_edit_rerun_does_not_shadow_the_canonical_breaker(client, db_session):
    """A prompt-edit re-runs the whole task under a new prompt (a NEWER full run with
    its own replay payload) — shown transiently in-session — but the breaker's
    CANONICAL run (the original) stays what persisted-review reopens, so a curated
    breaker never turns into whatever a prompt edit produced."""
    from app.api.gym import _persist_gym_review

    task_id = "M94/canonical_breaker"
    orig_run, orig_review = _synthetic_gym_review(task_id, success=False)  # canonical breaker (3 steps, reward 0)
    _persist_gym_review(db_session, task_id, "openai", orig_run, orig_review)

    # annotator edits the prompt → a newer FULL run (with raw), which happens to solve it
    edit_run, edit_review = _synthetic_gym_review(task_id, success=True)
    edit_review["steps"] = edit_review["steps"][:1]  # a different, shorter run
    _persist_gym_review(db_session, task_id, "openai", edit_run, edit_review, brief="please double-check first")

    # reopening the breaker STILL shows the canonical breaking run, not the edit
    canon = client.get(f"/api/gym/tasks/{task_id}/persisted-review").json()
    assert len(canon["steps"]) == 3 and canon["gymReward"] == 0, "canonical breaker must survive a prompt edit"


def test_gym_verdict_is_isolated_per_annotator(client_for, db_session):
    """The cross-annotator leak fix: an UNcorrected annotator scores from the shared
    CANONICAL breaker; a corrected annotator scores from THEIR OWN correction —
    never from another annotator's correction verdict."""
    from app.api.gym import _persist_gym_review

    task = "M90/verdict_iso"
    # canonical breaker (reward 0) — the shared run everyone starts from
    r0, rev0 = _synthetic_gym_review(task, success=False)
    _persist_gym_review(db_session, task, "openai", r0, rev0)
    suite = [{"id": v["id"], "level": v["level"], "assertion": v["assertion"], "code": v["code"],
              "check": None, "failsUntilCorrected": False, "placeholder": False,
              "addedByHuman": False, "gymResult": v.get("gymResult")} for v in rev0["verifiers"]]

    cx, cy = client_for("x@iso.io"), client_for("y@iso.io")
    sx = cx.post(f"/api/tasks/{task}/sessions", json={"fresh": True}).json()["sessionId"]
    sy = cy.post(f"/api/tasks/{task}/sessions", json={"fresh": True}).json()["sessionId"]

    # X corrects: mark a rerun point + persist X's correction run (reward 1) linked to sx
    cx.patch(f"/api/sessions/{sx}", json={"rerunFrom": 1})
    rc, revc = _synthetic_gym_review(task, success=True)
    _persist_gym_review(db_session, task, "openai", rc, revc, persist_raw=False, origin_session_id=sx)

    def bench(c, sid, corrected):
        c.put(f"/api/sessions/{sid}/suite", json={"verifiers": suite})
        c.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})  # scoring is gated on review
        return c.post(f"/api/sessions/{sid}/run", json={"corrected": corrected, "verifiers": [], "overrides": []}).json()["reward"]

    assert bench(cx, sx, True) == 1   # X scores from X's OWN correction
    assert bench(cy, sy, False) == 0  # Y scores from the CANONICAL breaker, NOT X's correction


def test_zero_action_run_with_a_verdict_is_not_a_failure():
    """Answering / clarifying / refusing instead of clicking is the CORRECT outcome
    for much of this dataset. A run with zero actions but a REAL milestone verdict
    must reach the annotator, not be discarded as 'no trajectory'. Only a run with
    neither actions nor a verdict is a genuine failure."""
    from app.api.gym import _resume_run_job
    from unittest import mock

    import pytest

    from app import gym_client, gym_review, jobs

    verdict = {"score": 1.0, "success": False, "missed_milestones": ["clarified_with_user"]}

    # zero steps BUT a verdict → accepted (no JobFailure raised before to_review)
    ok_payload = {"trajectory": {"steps": [], "verifier_result": verdict, "task_brief": "b"}, "seed": 0}
    with mock.patch.object(gym_client, "resume_run", return_value=ok_payload), \
         mock.patch.object(gym_client, "state", return_value={}), \
         mock.patch.object(gym_client, "world", return_value={}), \
         mock.patch.object(gym_review, "to_review", return_value={"task": {"prompt": "p"}, "steps": [], "verifiers": []}):
        out = _resume_run_job("M1/x", 0, {}, "/", 1, "openai")
    assert out["gymReward"] == 0  # verdict says success=False
    assert out["steps"] == []     # zero actions is a legitimate outcome

    # zero steps AND no verdict → still a real failure
    with mock.patch.object(gym_client, "resume_run", return_value={"trajectory": {"steps": []}}):
        with pytest.raises(jobs.JobFailure):
            _resume_run_job("M1/x", 0, {}, "/", 1, "openai")


def test_unproven_human_verifier_cannot_ride_a_passing_gym_verdict(client, db_session):
    """Reward 1 must not coexist with a verifier the annotator added that has no
    executable check — the human suite is what ships and what a lab re-runs."""
    from app.api.gym import _persist_gym_review

    task = "M92/human_suite_gate"
    run, review = _synthetic_gym_review(task, success=True)      # gym says PASS
    _persist_gym_review(db_session, task, "openai", run, review)

    sid = client.post(f"/api/tasks/{task}/sessions", json={"fresh": True}).json()["sessionId"]
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    suite = [{"id": v["id"], "level": v["level"], "assertion": v["assertion"], "code": v["code"],
              "check": None, "failsUntilCorrected": False, "placeholder": False,
              "addedByHuman": False, "gymResult": v.get("gymResult")} for v in review["verifiers"]]
    # gym milestones alone → the environment verdict stands
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": suite})
    assert client.post(f"/api/sessions/{sid}/run", json={"overrides": []}).json()["reward"] == 1

    # the annotator adds a check they haven't made executable yet
    suite.append({"id": "add-1", "level": "backend", "assertion": "refund really issued",
                  "code": "/* define check */", "check": None, "failsUntilCorrected": False,
                  "placeholder": True, "addedByHuman": True})
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": suite})
    out = client.post(f"/api/sessions/{sid}/run", json={"overrides": []}).json()
    assert out["reward"] == 0, "an unproven human check must not be masked by the gym verdict"
    assert out["results"]["add-1"] == "fail"
