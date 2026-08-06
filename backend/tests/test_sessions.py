"""Session lifecycle over the real API (SQLite-backed TestClient)."""


def _open(client, task="GYM-2041", reviewed=True):
    r = client.post(f"/api/tasks/{task}/sessions", json={})
    assert r.status_code == 200
    sid = r.json()["sessionId"]
    if reviewed:
        # Walk the run like an annotator: the server now gates the benchmark on
        # persisted review progress, so scoring an unreviewed run is a 409.
        n = len(client.get(f"/api/tasks/{task}/review").json()["steps"])
        client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": n})
    return sid


def _verifiers(client, task="GYM-2041"):
    return client.get(f"/api/tasks/{task}/review").json()["verifiers"]


def test_open_session_creates_draft(client):
    body = client.post("/api/tasks/GYM-2041/sessions", json={}).json()
    assert body["status"] == "draft"
    assert body["taskExternalId"] == "GYM-2041"


def test_open_unknown_task_404(client):
    assert client.post("/api/tasks/NOPE/sessions", json={}).status_code == 404


def test_full_lifecycle_writes_a_submission(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    client.patch(f"/api/sessions/{sid}", json={"status": "steps_approved"})
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    run = client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert run.json()["reward"] == 1
    sub = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False, "kind": "golden"})
    assert sub.status_code == 200
    assert sub.json()["status"] == "submitted"
    assert sub.json()["submission"]["reward"] == 1


def test_run_original_state_is_reward_zero(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    run = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": vs, "overrides": []})
    assert run.json()["reward"] == 0


def test_submit_requires_reward_one_or_override(client):
    sid = _open(client)
    r = client.post(f"/api/sessions/{sid}/submit", json={"reward": 0, "override": False})
    assert r.status_code == 409


def test_rerun_falls_back_to_deterministic_without_key(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    r = client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay paypal", "mode": "agent"})
    assert r.json()["mode"] == "deterministic"
    assert len(r.json()["steps"]) >= 1


def test_session_runs_against_its_own_task(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client, "GYM-2042")
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 6, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    vs = _verifiers(client, "GYM-2042")
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    run = client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert run.json()["reward"] == 1


# ---- reward integrity: the server scores the PERSISTED suite, never the client ----


def test_run_ignores_client_supplied_verifiers(client, monkeypatch):
    """A fabricated trivially-passing check in the request body must NOT inflate
    the reward — the server evaluates the persisted suite (original state ⇒ 0)."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    fabricated = [{"id": "x", "level": "ui", "assertion": "always", "code": "1",
                   "check": {"kind": "trace_max_steps", "n": 9999}}]
    run = client.post(f"/api/sessions/{sid}/run",
                      json={"corrected": False, "verifiers": fabricated, "overrides": []})
    assert run.json()["reward"] == 0  # persisted suite on original state, not the bogus check


def test_run_requires_a_saved_suite(client):
    sid = _open(client)
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    r = client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": [], "overrides": []})
    assert r.status_code == 409


def test_submit_stores_server_reward_not_client_asserted(client, monkeypatch):
    """Client claims reward 1 on an original-state (reward 0) run. Submit must use
    the server reward: 409 without override, and store 0 (breaker) with override."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": vs, "overrides": []})
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).status_code == 409
    sub = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": True, "overrideReason": "x"})
    assert sub.status_code == 200
    assert sub.json()["submission"]["reward"] == 0  # server-computed, not the client's 1
    assert sub.json()["submission"]["kind"] == "breaker"


def test_submit_requires_a_prior_benchmark_run(client):
    sid = _open(client)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    r = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": True, "overrideReason": "x"})
    assert r.status_code == 409  # must run the benchmark first


# ---- start-new session, submit-once, and the trajectory record ----


def test_fresh_starts_a_new_session(client):
    sid1 = _open(client)
    resumed = client.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"]
    assert resumed == sid1  # default resumes the latest
    fresh = client.post("/api/tasks/GYM-2041/sessions", json={"fresh": True}).json()["sessionId"]
    assert fresh != sid1  # fresh=true starts a new one


def test_submit_locks_the_session(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).status_code == 200
    # A submitted session is immutable — no superseding submission.
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 0, "override": True, "overrideReason": "x"}).status_code == 409


def test_patch_cannot_set_submitted_or_out_of_range(client):
    sid = _open(client)
    assert client.patch(f"/api/sessions/{sid}", json={"status": "submitted"}).status_code == 400  # only /submit does that
    assert client.patch(f"/api/sessions/{sid}", json={"rerunFrom": -5}).status_code == 422


def test_submitted_session_is_immutable_via_patch_and_rerun(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).status_code == 200
    # the submit lock is now enforced on BOTH reopen paths (was the blocker)
    assert client.patch(f"/api/sessions/{sid}", json={"status": "draft"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 5, "correction": "x"}).status_code == 409


def test_rerun_rejects_out_of_range_step(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    assert client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 9999, "correction": "x"}).status_code == 422


def test_malformed_check_ir_fails_closed_not_500(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    bad = [{"id": "m", "level": "backend", "assertion": "x", "code": "c", "check": {"kind": "state_lte", "path": "p"}}]  # missing 'value'
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": bad})
    r = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": bad, "overrides": []})
    assert r.status_code == 200                       # not a 500
    assert r.json()["results"]["m"] == "fail"         # malformed IR failed closed


def test_safety_override_flags_the_sample_and_derives_provenance(client, monkeypatch):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    # original state fails the SAFETY check sa1; override it to force reward 1
    r = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": vs, "overrides": ["sa1"]})
    assert r.json()["reward"] == 1
    sub = client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).json()["submission"]
    assert sub["kind"] == "flagged"      # NOT a clean golden — a safety check was overridden
    assert sub["override"] is True       # provenance derived from the run, not the client flag


def test_open_session_records_a_trajectory(client, db_session):
    from app.models import Trajectory, TrajectoryStep

    sid = _open(client)
    trajs = db_session.query(Trajectory).all()
    assert len(trajs) == 1
    assert str(trajs[0].session_id) == sid
    assert db_session.query(TrajectoryStep).count() > 0  # the fixture trace is recorded


# ---- Cluster A: the immutability lock covers EVERY mutating endpoint ----


def _submitted_golden(client, monkeypatch, task="GYM-2041"):
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client, task)
    vs = _verifiers(client, task)
    # `corrected` is DERIVED from a persisted correction now — a golden requires a
    # real fork, not a client-asserted flag.
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).status_code == 200
    return sid, vs


def test_submitted_session_locks_suite_run_and_benchmark(client, monkeypatch):
    """The lock must ride on suite/run/benchmark too — otherwise a locked sample's
    scored suite could be rewritten and the exported bundle would drift."""
    sid, vs = _submitted_golden(client, monkeypatch)
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "overrides": []}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/benchmark", json={"corrected": False}).status_code == 409


# ---- Cluster C: input validation returns 4xx, never a 500 ----


def test_duplicate_verifier_ids_rejected(client):
    sid = _open(client)
    dup = [
        {"id": "x", "level": "ui", "assertion": "a", "code": "c", "check": {"kind": "state_true", "path": "order.placed"}},
        {"id": "x", "level": "ui", "assertion": "b", "code": "c", "check": {"kind": "state_true", "path": "order.placed"}},
    ]
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": dup}).status_code == 422


def test_oversized_inputs_return_422_not_500(client):
    # annotatorEmail bounded to 255, verifier id bounded to 64
    assert client.post("/api/tasks/GYM-2041/sessions", json={"annotatorEmail": "x" * 300 + "@t.co"}).status_code == 422
    sid = _open(client)
    big = [{"id": "y" * 100, "level": "ui", "assertion": "a", "code": "c", "check": {"kind": "state_true", "path": "order.placed"}}]
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": big}).status_code == 422


# ---- Cluster B: one submission per session, enforced at the DB layer ----


def test_only_one_submission_per_session(client, monkeypatch, db_session):
    from uuid import UUID

    from app.models import Submission

    sid, _ = _submitted_golden(client, monkeypatch)
    # a second submit is blocked, and the DB holds exactly one row for the session
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 0, "override": True, "overrideReason": "x"}).status_code == 409
    n = db_session.query(Submission).filter(Submission.session_id == UUID(sid)).count()
    assert n == 1


# ---- audit fixes: NUL bytes 422 (not 500) + race-safe get-or-create ----


def test_nul_byte_in_text_fields_is_422_not_500(client):
    assert client.post("/api/tasks/GYM-2041/sessions", json={"annotatorEmail": "a\x00b@x.io"}).status_code == 422
    sid = _open(client)
    assert client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 2, "correction": "bad\x00fix"}).status_code == 422
    bad = [{"id": "x", "level": "ui", "assertion": "n\x00ul", "code": "c", "check": {"kind": "state_true", "path": "a"}}]
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": bad}).status_code == 422
    # a NUL nested inside the check IR is also rejected
    bad2 = [{"id": "y", "level": "ui", "assertion": "a", "code": "c", "check": {"kind": "dom_contains", "needle": "x\x00y"}}]
    assert client.put(f"/api/sessions/{sid}/suite", json={"verifiers": bad2}).status_code == 422


def test_sessions_are_isolated_per_annotator(client_for, db_session):
    from app.models import Annotator

    ca, cb = client_for("alice@x.io"), client_for("bob@x.io")
    sa = ca.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"]
    sb = cb.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"]
    assert sa != sb  # each annotator gets their OWN session for the shared task

    # resume: alice re-opening (not fresh) returns HER same session, not bob's
    assert ca.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"] == sa

    # OWNERSHIP: bob cannot read or mutate alice's session (a UUID isn't enough)
    assert cb.get(f"/api/sessions/{sa}").status_code == 404
    assert cb.patch(f"/api/sessions/{sa}", json={"reviewedThrough": 1}).status_code == 404
    assert cb.post(f"/api/sessions/{sa}/submit", json={"reward": 1}).status_code == 404
    assert ca.get(f"/api/sessions/{sa}").status_code == 200  # alice still owns hers

    assert db_session.query(Annotator).filter(Annotator.email.in_(["alice@x.io", "bob@x.io"])).count() == 2


def test_unlimited_corrections_build_a_versioned_branch_chain(client, db_session):
    """The annotator can correct as many times as they like: each round persists its
    OWN immutable branch, chained to the previous one (parent_id), with its own
    correction text — so the full iteration history survives, and reopening restores
    the LATEST round's trace."""
    from uuid import UUID

    from app.models import AuditLog, ReviewSession, TrajectoryBranch
    from sqlalchemy import select

    sid = client.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"]
    sid_u = UUID(sid)

    rounds = [
        {"fromStep": 2, "steps": [{"idx": 3, "type": "click", "tabId": "shop", "description": "r1-a"},
                                  {"idx": 4, "type": "click", "tabId": "shop", "description": "r1-b"}],
         "correction": "round 1: check the price first"},
        {"fromStep": 3, "steps": [{"idx": 4, "type": "click", "tabId": "shop", "description": "r2-a"},
                                  {"idx": 5, "type": "submit", "tabId": "shop", "description": "r2-b"}],
         "correction": "round 2: now open the order"},
        {"fromStep": 4, "steps": [{"idx": 5, "type": "submit", "tabId": "shop", "description": "r3-a"}],
         "correction": "round 3: decline the bogus refund"},
    ]
    for r in rounds:
        assert client.post(f"/api/sessions/{sid}/rerun-gym", json={**r, "mode": "agent"}).status_code == 200

    # every round persisted its own branch, newest last
    branches = db_session.scalars(
        select(TrajectoryBranch).where(TrajectoryBranch.session_id == sid_u)
        .order_by(TrajectoryBranch.created_at)
    ).all()
    assert len(branches) == 3, "each correction must persist its own branch (full history kept)"

    # chained: b1 is the root, each later round points at its predecessor
    assert branches[0].parent_id is None
    assert branches[1].parent_id == branches[0].id
    assert branches[2].parent_id == branches[1].id

    # each round kept its OWN instruction + fork point (nothing overwritten)
    assert [b.from_step for b in branches] == [2, 3, 4]
    assert [b.correction for b in branches] == [r["correction"] for r in rounds]
    assert branches[0].steps["steps"][0]["description"] == "r1-a"  # round 1 still intact

    # the session tracks the LATEST round, and re-locks for re-review
    s = db_session.get(ReviewSession, sid_u)
    assert s.rerun_from == 4 and s.status == "draft"

    # reopening restores the latest round's trace (not an earlier one)
    snap = client.get(f"/api/sessions/{sid}").json()
    assert snap["rerunFrom"] == 4
    assert [st["description"] for st in snap["branch"]["steps"]] == ["r3-a"]

    # one audit breadcrumb per correction
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.session_id == sid_u, AuditLog.action == "agent.rerun_gym")
    ).all()
    assert len(audits) == 3


def test_history_returns_every_round_in_order(client, client_for):
    """The history endpoint surfaces the FULL iteration chain (oldest → newest) —
    each round with its own fork point, instruction and steps — and is private to
    the owning annotator."""
    sid = client.post("/api/tasks/GYM-2041/sessions", json={}).json()["sessionId"]
    for i, frm in enumerate([2, 3, 4], start=1):
        client.post(f"/api/sessions/{sid}/rerun-gym", json={
            "fromStep": frm, "mode": "agent", "correction": f"round {i}",
            "steps": [{"idx": frm + 1, "type": "click", "tabId": "shop", "description": f"r{i}-a"}],
        })

    rounds = client.get(f"/api/sessions/{sid}/history").json()["rounds"]
    assert [r["round"] for r in rounds] == [1, 2, 3]
    assert [r["fromStep"] for r in rounds] == [2, 3, 4]
    assert [r["correction"] for r in rounds] == ["round 1", "round 2", "round 3"]
    assert rounds[0]["steps"][0]["description"] == "r1-a"  # earliest round still viewable
    assert all(r["stepCount"] == 1 and r["at"] for r in rounds)

    # another annotator cannot read this session's history
    other = client_for("someone-else@deccan.ai")
    assert other.get(f"/api/sessions/{sid}/history").status_code == 404


def test_client_cannot_fabricate_a_golden_by_claiming_corrected(client, monkeypatch):
    """`corrected` selects the corrected end-state for scoring, so a client claiming
    corrected=true WITHOUT a persisted correction could manufacture a reward-1
    'golden' sample. It is now derived from the DB and the claim is ignored."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client)                       # reviewed, but NOT corrected
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})

    run = client.post(f"/api/sessions/{sid}/run", json={"corrected": True, "verifiers": vs, "overrides": []})
    assert run.json()["reward"] == 0, "claiming corrected must not score the corrected state"
    # ...and without a reward-1 benchmark the sample cannot be submitted as golden
    assert client.post(f"/api/sessions/{sid}/submit", json={"reward": 1, "override": False}).status_code == 409

    # doing the REAL correction earns the golden
    client.post(f"/api/sessions/{sid}/rerun", json={"fromStep": 12, "correction": "pay with the personal card", "mode": "deterministic"})
    client.patch(f"/api/sessions/{sid}", json={"reviewedThrough": 999})
    assert client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": vs, "overrides": []}).json()["reward"] == 1


def test_benchmark_requires_the_run_to_have_been_reviewed(client, monkeypatch):
    """The suite/benchmark are downstream of human review — gated on persisted
    progress, not a client-set status."""
    monkeypatch.setattr("app.agent.settings.anthropic_api_key", "")
    sid = _open(client, reviewed=False)
    vs = _verifiers(client)
    client.put(f"/api/sessions/{sid}/suite", json={"verifiers": vs})
    r = client.post(f"/api/sessions/{sid}/run", json={"corrected": False, "verifiers": vs, "overrides": []})
    assert r.status_code == 409 and "reviewed" in r.json()["detail"]


def test_a_reworded_brief_is_persisted(client):
    """`prompt_override` existed on the model and nothing ever wrote it, so an
    annotator's rewording lived in local state and vanished on remount — while
    being real signal about an instruction that was ambiguous."""
    sid = client.post("/api/tasks/GYM-2041/sessions", json={"fresh": True}).json()["sessionId"]
    r = client.patch(f"/api/sessions/{sid}", json={"promptOverride": "  cancel only the unshipped ones  "})
    assert r.status_code == 200
    assert r.json()["promptOverride"] == "cancel only the unshipped ones", "and it is trimmed"
    # survives a reload
    assert client.post("/api/tasks/GYM-2041/sessions", json={}).json()["promptOverride"] == \
        "cancel only the unshipped ones"
