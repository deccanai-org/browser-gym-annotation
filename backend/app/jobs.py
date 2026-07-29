"""Background job registry for slow work (a gym browser run, an agent branch)
that must run OFF the HTTP request path.

The in-process dict is a CACHE. The RECORD is a `background_job` row.

Why that way round. A jobId outlives the process that minted it. A gym run takes
up to four minutes and the client polls for five, treating any non-200 as a
transient blip and simply retrying (`pollGymJob`, frontend/src/lib/api.ts:398).
While `get()` looked only in process memory, every restart turned every
outstanding job into a 404 "unknown or expired job" — for runs that had already
finished, and for runs still executing. Nothing raised and nothing logged: the
annotator watched a spinner reach its own timeout and the result was lost.

Why a table of its own rather than AgentRunJob rows. AgentRunJob is the durable
record of ONE handoff — an agent branch run against an attempt — and it is the
ledger the rerun cap is charged from (`counts_against_cap`). Four of the five
kinds submitted here (run-review, capture-seeds, autogen-verifiers, resume-run)
have no attempt at all and return payloads it has nowhere to put, so hosting them
there would mean a nullable attempt_id, a kind column and a result column bolted
onto the cap ledger — non-agent work sitting inside an annotator's run budget,
where a miscount is invisible until somebody is refused a run they never used.
The two records stay distinct, and this store only ever READS AgentRunJob (see
`_from_agent_run`), so a poll of a run id still answers truthfully.

The model lives in this module, beside the store that owns it, the same way
`CanonicalRun` lives in app/canonical.py beside its resolver.

ONE WORKER. A single uvicorn process owns every thread it starts, which is what
lets `recover_orphans()` conclude that a non-terminal row stamped with an older
boot id has no worker. Running more than one worker means replacing that boot-id
test with a heartbeat lease — the boot id is here so that change stays local.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from sqlalchemy import JSON, String, Text, delete, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app import models
from app.db import Base

log = logging.getLogger("annotator.jobs")

JobStatus = Literal["queued", "running", "done", "error"]
TERMINAL = ("done", "error")

# Identifies THIS process. Stamped on every row we take work for, so a later boot
# can tell "still running, here" apart from "was running in a process that is now
# gone" — states the row alone cannot distinguish.
BOOT_ID = uuid.uuid4().hex

# How long a finished job stays answerable. Deliberately longer than the
# in-memory TTL: a client polling after the cache dropped the job must still get
# its result, not a 404 that reads as "this job never existed".
RETENTION = timedelta(days=7)

_RESTART_LOST = "the backend restarted while this job was running — nothing is executing it now"


class JobFailure(Exception):
    """An EXPECTED worker failure — maps to status='error' with this message."""


class BackgroundJob(Base):
    """The durable half of a job: what a poll reads once the process that ran it
    is gone."""

    __tablename__ = "background_job"

    # A plain hex string, not a Uuid column: this IS the jobId already handed to
    # the client, so the row must be reachable by the exact string they poll with.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    owner: Mapped[str] = mapped_column(String(32), default="", index=True)  # boot id of the running process
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())


def _jsonable(value: Any) -> Any:
    """A result the JSON column cannot hold must not cost the job its durable
    record. Keep the row, name what was dropped, and let this process's own
    clients go on reading the real object from the cache."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {"unserializable": type(value).__name__}


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = "queued"
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Public fields for a job hydrated from a DIFFERENT record: an agent run
    # carries version identity, never a review payload.
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        out: dict = {"jobId": self.id, "status": self.status}
        out.update(self.extra)
        if self.status == "done":
            out["review"] = self.result
        if self.status == "error":
            out["error"] = self.error
        return out


class JobStore:
    def __init__(self, ttl_seconds: float = 3600.0, *, session_factory: Callable[[], Session] | None = None):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._factory = session_factory

    # ---- durability, best effort -------------------------------------------
    def _sessions(self) -> Callable[[], Session]:
        """Resolved per call, not captured at import: a store constructed while
        the module graph is still loading must not pin a sessionmaker that a
        later rebind (deployment wiring, tests) would leave it ignoring."""
        if self._factory is not None:
            return self._factory
        from app.db import SessionLocal

        return SessionLocal

    def _txn(self, what: str, op: Callable[[Session], Any]) -> Any:
        """A database that is down degrades the RESTART guarantee; it must not
        kill a job that is running fine in this process. The warning is the honest
        signal — swallowing it silently is what let the in-memory store look like
        it worked."""
        try:
            with self._sessions()() as db:
                out = op(db)
                db.commit()
                return out
        except Exception as exc:  # noqa: BLE001
            log.warning("job store: %s did not persist (%s) — this job will not survive a restart", what, exc)
            return None

    def _persist(self, jid: str, **kw) -> None:
        def op(db: Session) -> None:
            row = db.get(BackgroundJob, jid)
            if row is None:  # its insert never landed (DB was down then, up now)
                return
            for k, v in kw.items():
                setattr(row, k, _jsonable(v) if k == "result" else v)
            row.owner = BOOT_ID
            row.updated_at = datetime.utcnow()

        self._txn(f"job {jid[:8]} → {kw.get('status', 'update')}", op)

    # ---- reads --------------------------------------------------------------
    def get(self, jid: str) -> Job | None:
        """This process's own view first, then the durable record, then the agent
        run ledger. A miss on all three genuinely means "no such job"."""
        with self._lock:
            j = self._jobs.get(jid)
        if j is not None:
            return j
        return self._from_durable(jid) or self._from_agent_run(jid)

    def _from_durable(self, jid: str) -> Job | None:
        def op(db: Session) -> tuple | None:
            row = db.get(BackgroundJob, jid)
            if row is None:
                return None
            if row.status not in TERMINAL and row.owner != BOOT_ID:
                # We are the only worker and this is not ours, so nothing is
                # running it. Answering "queued" here is the silent failure: the
                # client polls a job with no worker until its own timeout, then
                # reports nothing at all rather than a failure anyone can act on.
                row.status = "error"
                row.error = _RESTART_LOST
                row.updated_at = datetime.utcnow()
            return (row.kind, row.status, row.result, row.error)

        got = self._txn(f"orphan check for {jid[:8]}", op)
        if got is None:
            return None
        kind, status, result, error = got
        return Job(id=jid, kind=kind, status=status, result=result, error=error or None)

    def _from_agent_run(self, jid: str) -> Job | None:
        """An idempotent replay of `POST /sessions/{id}/versions/agent-run` answers
        with the AgentRunJob's OWN id as `jobId` (app/api/versions.py), so this
        route is polled with ids from a second id space. They resolve here — or the
        retry that idempotency exists to make safe gets a 404, and a client that
        cannot see its run re-fires with a fresh key, spawning precisely the second
        run the key was meant to prevent.
        """
        try:
            run_id = uuid.UUID(jid)
        except (ValueError, AttributeError, TypeError):
            return None

        def op(db: Session) -> tuple | None:
            row = db.get(models.AgentRunJob, run_id)
            if row is None:
                return None
            return (row.status, row.error, str(row.result_version_id or ""), str(row.attempt_id))

        got = self._txn(f"agent run lookup for {jid[:8]}", op)
        if got is None:
            return None
        status, error, version_id, attempt_id = got
        # The owning attempt rides on the job so the route can refuse a stranger.
        # An AgentRunJob id is a bare UUID from a second id space, and without this
        # any authenticated annotator could poll any other annotator's run.
        extra = {"runId": jid, "attemptId": attempt_id}
        if version_id:
            extra["versionId"] = version_id
        return Job(id=jid, kind="agent-branch", status=status, error=error or None, extra=extra)

    # ---- writes -------------------------------------------------------------
    def _gc(self) -> None:  # caller holds the lock
        cutoff = time.time() - self._ttl
        for jid in [j.id for j in self._jobs.values() if j.status in TERMINAL and j.updated_at < cutoff]:
            self._jobs.pop(jid, None)

    def _set(self, jid: str, **kw) -> None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return
            for k, v in kw.items():
                setattr(j, k, v)
            j.updated_at = time.time()
        # Outside the lock deliberately: a database round trip held under it would
        # serialise every job in the process behind the slowest write.
        self._persist(jid, **kw)

    def submit(self, kind: str, fn: Callable[..., Any], *args, **kwargs) -> Job:
        with self._lock:
            self._gc()
            job = Job(id=uuid.uuid4().hex, kind=kind)
            self._jobs[job.id] = job

        # Persisted BEFORE the worker starts. A crash in the first millisecond
        # must still leave a row to answer with, or the client polls an id the
        # server has no memory of ever issuing.
        self._txn(
            f"job {job.id[:8]} submit",
            lambda db: db.add(BackgroundJob(id=job.id, kind=kind, status=job.status, owner=BOOT_ID)),
        )

        def _run() -> None:
            self._set(job.id, status="running")
            try:
                result = fn(*args, **kwargs)
                self._set(job.id, status="done", result=result)
            except JobFailure as e:
                self._set(job.id, status="error", error=str(e))
            except Exception as e:  # noqa: BLE001
                self._set(job.id, status="error", error=f"internal error: {e}")

        threading.Thread(target=_run, name=f"job-{kind}-{job.id[:8]}", daemon=True).start()
        return job

    # ---- startup reconciliation ---------------------------------------------
    def recover_orphans(self, db: Session) -> int:
        """Fail every job left in flight by a process that is gone, and drop the
        records nobody can still be polling for.

        Called from the lifespan (app/main.py). A row that is neither done nor
        errored and carries a DIFFERENT boot id had its worker in a process that
        no longer exists; left alone it answers "running" to every poll forever
        and the run it stands for never lands. The caller commits.
        """
        rows = db.scalars(
            select(BackgroundJob).where(
                BackgroundJob.status.not_in(TERMINAL),
                BackgroundJob.owner != BOOT_ID,
            )
        ).all()
        for row in rows:
            row.status = "error"
            row.error = _RESTART_LOST
            row.updated_at = datetime.utcnow()
        db.execute(delete(BackgroundJob).where(BackgroundJob.updated_at < datetime.utcnow() - RETENTION))
        db.flush()
        return len(rows)


store = JobStore()  # module-level singleton (one uvicorn worker owns all jobs)
