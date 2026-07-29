"""Restart safety: what the backend reclaims when it comes back up.

Both gaps covered here failed SILENTLY. A workspace lease left "ready" after a
restart hands `endpoint_for()` the address of a gym process that no longer
exists, and leaks the one it forgot to terminate. A job left in process memory
answers 404, and the client reads a non-200 as a transient blip and polls past it
to its own five-minute timeout (`pollGymJob`, frontend/src/lib/api.ts) — so a run
that finished is reported as nothing at all. Neither raises; both are noticed
weeks later as a leak or a missing result.

So every test here crosses a restart boundary and then reads back through the
path a client actually uses, rather than asserting that a reconciler works when
called by hand — which it already did, from nowhere.

WHAT "RESTART" MEANS HERE. A new process changes three things, and `boot` models
all three: a fresh `app.jobs.BOOT_ID` and an empty `app.jobs.store` (both are
module-level in app/jobs.py, so both are per-process), against the same database
FILE on disk. The lifespan is not simulated — entering `TestClient(app)` runs the
real one, which is also what makes these tests fail if the hooks are ever unwired
again.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import agent_runs, auth as authmod, db as db_module, jobs, main, models, versions, workspace
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.workspace import manager
from app.workspace.provider import WorkspaceHandle

ANNOTATOR = "durability@deccan.ai"


class FakeRuntime:
    """The runtime provider contract from app/workspace/provider.py, with `alive`
    keyed by `external_ref` so a test can kill a workspace behind the manager's
    back — which is exactly what a restart does to a `local_process` gym.

    `provision` raises: reconciliation reclaims, it never starts anything, and a
    fake that quietly provisioned would hide the difference.
    """

    kind = "local_process"

    def __init__(self, alive: dict[str, bool] | None = None) -> None:
        self.alive = dict(alive or {})
        self.terminated: list[str] = []

    def provision(self, *, label: str) -> WorkspaceHandle:
        raise AssertionError(f"reconciliation must never provision (asked for {label})")

    def health(self, handle: WorkspaceHandle) -> bool:
        return self.alive.get(handle.external_ref, False)

    def terminate(self, handle: WorkspaceHandle) -> None:
        self.terminated.append(handle.external_ref)
        self.alive[handle.external_ref] = False


@pytest.fixture()
def boot(tmp_path, monkeypatch):
    """A callable context manager: each `with boot() as client` is one process
    lifetime against one database on disk."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'durability.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(models.Annotator(email=ANNOTATOR, password_hash=authmod.hash_password("testpass1")))
        db.commit()

    @contextmanager
    def _boot():
        # Everything the process boundary resets. `app.db.SessionLocal` is looked
        # up per call by both the lifespan and the job store, so rebinding it here
        # is what points the REAL startup path at this database.
        monkeypatch.setattr(db_module, "SessionLocal", factory)
        monkeypatch.setattr(main, "engine", engine)
        monkeypatch.setattr(jobs, "BOOT_ID", uuid4().hex)
        monkeypatch.setattr(jobs, "store", jobs.JobStore())

        def _override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _override
        try:
            with TestClient(app) as c:
                c.cookies.set(settings.auth_cookie, authmod.make_token(ANNOTATOR))
                yield c
        finally:
            app.dependency_overrides.clear()

    _boot.sessions = factory
    return _boot


def _attempt(db, tag: str) -> models.ReviewSession:
    task = models.Task(external_id=f"DUR-{tag}-{uuid4().hex[:6]}", title="t", prompt="p", source="gym")
    ann = db.scalar(select(models.Annotator).where(models.Annotator.email == ANNOTATOR))
    db.add(task)
    db.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db.add(s)
    db.flush()
    t = models.Trajectory(session_id=s.id, agent="gpt-5.5", source="gym")
    db.add(t)
    db.commit()
    return s


def _lease(db, attempt_id: UUID, *, ref: str, endpoint: str) -> models.WorkspaceLease:
    lease = models.WorkspaceLease(
        attempt_id=attempt_id, purpose=manager.HUMAN, runtime_kind="local_process",
        status="ready", endpoint=endpoint, external_ref=ref,
    )
    db.add(lease)
    db.commit()
    return lease


def _poll(client, job_id: str, tries: int = 100) -> dict:
    for _ in range(tries):
        body = client.get(f"/api/gym/jobs/{job_id}").json()
        if body["status"] in jobs.TERMINAL:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached a terminal status")


# --------------------------------------------------------------------------- wiring
def test_the_lifespan_calls_every_reconciler(boot, monkeypatch):
    """Both reconcilers shipped fully tested and called by NOTHING, which is a
    green suite over an app that leaks on every restart. Assert the wiring from
    the application itself, not from the handler."""
    called: list[str] = []

    def _spy(name):
        def _fn(_db, **_kw):
            called.append(name)
            return 0
        return _fn

    def _store_spy(_self, _db):
        called.append("jobs")
        return 0

    monkeypatch.setattr(workspace, "reconcile_on_startup", _spy("leases"))
    monkeypatch.setattr(agent_runs, "reap_stale", _spy("stale runs"))
    monkeypatch.setattr(jobs.JobStore, "recover_orphans", _store_spy)

    with boot():
        pass

    assert called == ["leases", "stale runs", "jobs"], (
        "every reclaim must run at boot, leases first — until they do, `endpoint_for` "
        "can hand out the address of a process that died with the last boot"
    )


# --------------------------------------------------------------------------- leases
def test_a_restart_reclaims_the_lease_whose_process_is_gone(boot, monkeypatch):
    """The orphan is terminated and stops being served; the survivor is adopted.
    Reconciliation that only did the first half would reap live annotations."""
    runtime = FakeRuntime(alive={"pid-live": True, "pid-dead": False})
    monkeypatch.setattr(manager, "_provider", lambda: runtime)
    monkeypatch.setattr(manager, "isolation_available", lambda: True)

    with boot():
        with boot.sessions() as db:
            dead_attempt = _attempt(db, "dead")
            live_attempt = _attempt(db, "live")
            _lease(db, dead_attempt.id, ref="pid-dead", endpoint="http://127.0.0.1:9001")
            _lease(db, live_attempt.id, ref="pid-live", endpoint="http://127.0.0.1:9002")
            dead_id, live_id = dead_attempt.id, live_attempt.id

    with boot():
        with boot.sessions() as db:
            statuses = dict(
                db.execute(
                    select(models.WorkspaceLease.external_ref, models.WorkspaceLease.status)
                ).all()
            )
            assert statuses == {"pid-dead": "terminated", "pid-live": "ready"}
            # The read path every gym call goes through, not the row.
            assert manager.endpoint_for(db, dead_id).base_url == settings.gym_url, (
                "a lease whose process is gone must stop being handed out"
            )
            assert manager.endpoint_for(db, live_id).base_url == "http://127.0.0.1:9002"

    assert runtime.terminated == ["pid-dead"], (
        "the forgotten gym process is never reclaimed unless startup terminates it"
    )


# --------------------------------------------------------------------------- jobs
def test_a_job_whose_worker_died_reports_failed_not_missing(boot):
    """404 is the dangerous answer: the client treats it as a blip and keeps
    polling a job nobody is running until its own timeout expires."""
    # Never set. A real restart kills the worker thread; here it stays parked, so
    # releasing it would let a thread from the DEAD process write to the row the
    # restart just reconciled — which no restarted backend can do.
    parked = threading.Event()
    started = threading.Event()

    def _still_running():
        started.set()
        parked.wait(15)
        return {"never": "returned"}

    with boot() as c:
        job = jobs.store.submit("run-review", _still_running)
        assert started.wait(5)
        assert c.get(f"/api/gym/jobs/{job.id}").json()["status"] in ("queued", "running")

    with boot() as c:
        res = c.get(f"/api/gym/jobs/{job.id}")

    assert res.status_code == 200, "a 404 here is indistinguishable from a network blip"
    body = res.json()
    assert body["status"] == "error"
    assert "restarted" in body["error"], body


def test_a_job_that_finished_before_the_restart_still_returns_its_result(boot):
    """Truthful means truthful in both directions: the restart sweep must not
    turn a completed run into a failure. The result is the whole point of the
    poll — losing it silently discards a four-minute gym run."""
    with boot() as c:
        job = jobs.store.submit("run-review", lambda: {"task": {"id": "M40_bogus_pricematch"}})
        assert _poll(c, job.id)["status"] == "done"

    with boot() as c:
        body = c.get(f"/api/gym/jobs/{job.id}").json()

    assert body["status"] == "done"
    assert body["review"] == {"task": {"id": "M40_bogus_pricematch"}}


def test_the_sweep_leaves_this_processs_own_jobs_alone(boot):
    """A reclaim that cannot tell "gone" from "still going" would fail every live
    run the moment anything called it twice."""
    parked = threading.Event()
    started = threading.Event()

    def _still_running():
        started.set()
        parked.wait(15)
        return {"never": "returned"}

    with boot() as c:
        job = jobs.store.submit("run-review", _still_running)
        assert started.wait(5)
        with boot.sessions() as db:
            assert jobs.store.recover_orphans(db) == 0, "a job this boot is running is not an orphan"
            db.commit()
        assert c.get(f"/api/gym/jobs/{job.id}").json()["status"] == "running"


def test_an_unknown_job_id_is_still_a_404(boot):
    """The durable fallback must not turn every id into a plausible-looking job."""
    with boot() as c:
        assert c.get("/api/gym/jobs/deadbeef").status_code == 404
        assert c.get(f"/api/gym/jobs/{uuid4()}").status_code == 404


# --------------------------------------------------------------------------- rerun budget
def test_a_run_killed_by_the_restart_is_refunded_not_charged(boot):
    """The annotator got no model attempt out of a run the backend dropped, so it
    must not cost them one. A restart drops both shapes — a run that was executing,
    and one whose worker died before its first heartbeat — and the lifespan reaps
    through `agent_runs.reap_stale`, so this also pins that it still covers both."""
    with boot():
        with boot.sessions() as db:
            attempt = _attempt(db, "cap")
            executing = models.AgentRunJob(
                attempt_id=attempt.id, status=agent_runs.RUNNING,
                heartbeat_at=datetime.utcnow(), counts_against_cap=True,
            )
            never_started = models.AgentRunJob(
                attempt_id=attempt.id, status=agent_runs.QUEUED, counts_against_cap=True,
            )
            db.add_all([executing, never_started])
            db.commit()
            attempt_id, ids = attempt.id, (executing.id, never_started.id)

    with boot():
        with boot.sessions() as db:
            for run_id in ids:
                row = db.get(models.AgentRunJob, run_id)
                assert row.status == agent_runs.ERROR, "a run with no worker must not stay in flight"
                assert row.counts_against_cap is False, "an infrastructure failure must not burn a run"
            assert db.get(models.ReviewSession, attempt_id).agent_call_count == 0


def test_the_same_idempotency_key_does_not_spawn_a_second_run_after_a_restart(boot):
    """The key is the annotator's protection against a retry costing a second run.
    It has to hold across the boundary where retries actually happen."""
    with boot():
        with boot.sessions() as db:
            attempt = _attempt(db, "idem")
            root = versions.create_root(db, attempt_id=attempt.id, base_trajectory_id=attempt.trajectories[0].id)
            cp = models.EnvironmentCheckpoint(attempt_id=attempt.id, world={"step": 0}, step_clock=0)
            db.add(cp)
            db.flush()
            step = models.TrajectoryStep(
                trajectory_id=attempt.trajectories[0].id, idx=0, action_type="click",
                description="agent 0", actor="agent", before_checkpoint_id=cp.id,
            )
            db.add(step)
            db.flush()
            versions.adopt_steps(db, root, [step])
            run, _child = agent_runs.enqueue(
                db, attempt=attempt, source_version=root, step=step, idempotency_key="retry-me"
            )
            agent_runs.start(db, run, owner="api")
            db.commit()
            attempt_id, root_id, step_id, run_id = attempt.id, root.id, step.id, run.id

    with boot() as c:
        with boot.sessions() as db:
            attempt = db.get(models.ReviewSession, attempt_id)
            again, _ = agent_runs.enqueue(
                db, attempt=attempt, source_version=db.get(models.TrajectoryVersion, root_id),
                step=db.get(models.TrajectoryStep, step_id), idempotency_key="retry-me",
            )
            db.commit()
            assert again.id == run_id, "the retry must resolve to the run that already exists"
            rows = db.scalars(
                select(models.AgentRunJob).where(models.AgentRunJob.attempt_id == attempt_id)
            ).all()
            assert [r.id for r in rows] == [run_id], "a second row here is a second gym run"
            assert attempt.agent_call_count == 0

        # The replay path answers with the AgentRunJob's OWN id as `jobId`
        # (app/api/versions.py). A 404 on that id is what pushes a client into
        # re-firing with a fresh key — the second run this test exists to prevent.
        body = c.get(f"/api/gym/jobs/{run_id}").json()
        assert body["status"] == agent_runs.ERROR
        assert body["runId"] == str(run_id)


# --------------------------------------------------------------------------- degradation
def test_a_database_that_is_down_at_boot_degrades_instead_of_crashing(monkeypatch):
    """The reconcilers run before the first request is served. If one could raise
    out of the lifespan, a Postgres blip would turn every restart into an outage —
    so the existing bootstrap swallows and logs, and these must match it."""
    dead = create_engine("postgresql+psycopg://127.0.0.1:59999/nope")
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=dead))
    monkeypatch.setattr(main, "engine", dead)
    monkeypatch.setattr(jobs, "store", jobs.JobStore())

    with TestClient(app) as c:  # the lifespan runs here; it must not raise
        health = c.get("/health")
        assert health.status_code == 200
        assert health.json()["db"] == "down"
        # And a job submitted with no database still runs in this process.
        job = jobs.store.submit("run-review", lambda: {"ok": True})
        for _ in range(100):
            if jobs.store.get(job.id).status in jobs.TERMINAL:
                break
            time.sleep(0.02)
        assert jobs.store.get(job.id).status == "done"
