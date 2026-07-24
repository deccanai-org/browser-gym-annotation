"""Workspace lease lifecycle.

Two runtime types, and they are never shared:

* **Human workspace** — long-lived, leased to an attempt for the duration of the
  annotator's work. This is what the live browser attaches to.
* **Agent branch worker** — short-lived, provisioned from a checkpoint for one
  batch run and torn down. An agent run must NEVER execute against the human's
  workspace: the gym has one global ``SESSION`` per process, so a run would reset
  the world out from under the annotator mid-review.

Leases live in Postgres rather than process memory, so a backend restart can
reconcile (or reclaim) what it previously spawned instead of leaking processes.
TTL is INACTIVITY-based and extended by human control or a running job, so a
long-but-active annotation is never reaped mid-work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.gym_client import GymEndpoint
from app.workspace.provider import (
    DockerRuntimeProvider,
    LocalProcessRuntimeProvider,
    WorkspaceHandle,
    WorkspaceRuntimeProvider,
)

# Lease purposes. Kept distinct so the reaper and the capacity check can tell a
# human's workspace apart from a transient branch worker.
HUMAN = "human"
AGENT_BRANCH = "agent_branch"

_ACTIVE = ("provisioning", "ready")


class WorkspaceCapacityError(RuntimeError):
    """This annotator already holds the maximum number of workspaces.

    Distinct from a provisioning failure on purpose: a failure means fall back to
    the shared gym, whereas hitting the cap means the caller is being told to let
    something go first. Collapsing the two would silently hand the shared world to
    the person the cap was protecting everyone else from.
    """


def _live_count(db: Session, annotator_id: UUID) -> int:
    """Workspaces this annotator currently holds, across all attempts."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(models.WorkspaceLease)
            .where(
                models.WorkspaceLease.annotator_id == annotator_id,
                models.WorkspaceLease.status.in_(_ACTIVE),
            )
        )
        or 0
    )


def _provider() -> WorkspaceRuntimeProvider:
    """The runtime this deployment can actually use.

    `docker` is what a CONTAINERISED backend needs: the process provider spawns
    uvicorn from a gym checkout, and the backend image holds neither the gym
    source nor Playwright. The Kubernetes provider slots in here the same way.
    """
    if settings.workspace_runtime == "docker":
        return DockerRuntimeProvider()
    return LocalProcessRuntimeProvider()


def _handle_of(lease: models.WorkspaceLease) -> WorkspaceHandle:
    return WorkspaceHandle(
        endpoint=lease.endpoint,
        external_ref=lease.external_ref,
        runtime_kind=lease.runtime_kind,
        image_digest=lease.environment_image_digest,
    )


def _expiry() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=settings.workspace_idle_ttl_minutes)


def isolation_available() -> bool:
    """Isolation requires both the feature flag AND a usable gym checkout. If it
    is unavailable we fall back to the shared gym — but the caller must know, so
    two annotators are never silently placed in the same world."""
    return bool(settings.workspace_isolation) and _provider().available


def endpoint_for(db: Session, attempt_id: UUID | None) -> GymEndpoint:
    """The gym this attempt should talk to: its own leased workspace when one is
    ready, else the shared gym. Every gym call should go through this rather than
    reaching for ``settings.gym_url`` directly."""
    if attempt_id is not None and isolation_available():
        lease = active_lease(db, attempt_id, purpose=HUMAN)
        if lease is not None and lease.status == "ready" and lease.endpoint:
            return GymEndpoint(lease.endpoint)
    return GymEndpoint(settings.gym_url)


def active_lease(db: Session, attempt_id: UUID, *, purpose: str = HUMAN) -> models.WorkspaceLease | None:
    return db.scalar(
        select(models.WorkspaceLease)
        .where(
            models.WorkspaceLease.attempt_id == attempt_id,
            models.WorkspaceLease.status.in_(_ACTIVE),
            models.WorkspaceLease.purpose == purpose,
        )
        .order_by(models.WorkspaceLease.created_at.desc())
    )


def acquire(
    db: Session,
    attempt_id: UUID,
    *,
    annotator_id: UUID | None = None,
    purpose: str = HUMAN,
) -> models.WorkspaceLease | None:
    """Get-or-provision this attempt's workspace. Returns None when isolation is
    unavailable (caller falls back to the shared gym).

    Reuses a healthy lease; a lease whose process has died is marked terminated
    and replaced rather than handed back as a phantom endpoint.
    """
    if not isolation_available():
        return None

    existing = active_lease(db, attempt_id, purpose=purpose)
    if existing is not None:
        if existing.status == "ready" and _provider().health(_handle_of(existing)):
            touch(db, existing)
            return existing
        # Dead or half-provisioned — reclaim before replacing it.
        _terminate_row(db, existing, reason="unhealthy")

    # Capacity. Each workspace is a whole gym runtime — under `docker`, a real
    # container with a real memory footprint — so an unbounded acquire loop is a
    # way to fill the host, not merely a policy violation. Reap first: the usual
    # reason someone is at the cap is abandoned work, and reclaiming that is the
    # correct answer rather than refusing a live annotator.
    if annotator_id is not None and settings.workspace_max_per_annotator > 0:
        if _live_count(db, annotator_id) >= settings.workspace_max_per_annotator:
            reap_expired(db)
        if _live_count(db, annotator_id) >= settings.workspace_max_per_annotator:
            raise WorkspaceCapacityError(
                f"already holding {settings.workspace_max_per_annotator} workspaces — "
                "close one of your open attempts before starting another"
            )

    lease = models.WorkspaceLease(
        attempt_id=attempt_id,
        annotator_id=annotator_id,
        purpose=purpose,
        runtime_kind=settings.workspace_runtime,
        status="provisioning",
        expires_at=_expiry(),
    )
    db.add(lease)
    db.flush()  # need the id for the label before the slow spawn

    try:
        handle = _provider().provision(label=f"attempt-{attempt_id}-{purpose}-{lease.id}")
    except Exception as exc:  # noqa: BLE001 — a failed spawn must not wedge the attempt
        lease.status = "terminated"
        lease.terminated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        raise RuntimeError(f"workspace provisioning failed: {exc}") from exc

    lease.endpoint = handle.endpoint
    lease.external_ref = handle.external_ref
    lease.environment_image_digest = handle.image_digest
    lease.status = "ready"
    lease.last_active_at = datetime.now(timezone.utc).replace(tzinfo=None)
    lease.expires_at = _expiry()
    db.commit()
    db.refresh(lease)
    return lease


def touch(db: Session, lease: models.WorkspaceLease) -> None:
    """Extend the INACTIVITY window. Called on human control and while a job runs,
    so active work is never reaped out from under the annotator."""
    lease.last_active_at = datetime.now(timezone.utc).replace(tzinfo=None)
    lease.expires_at = _expiry()
    db.commit()


def release(db: Session, lease: models.WorkspaceLease) -> None:
    _terminate_row(db, lease, reason="released")


def _terminate_row(db: Session, lease: models.WorkspaceLease, *, reason: str) -> None:
    try:
        _provider().terminate(_handle_of(lease))
    finally:
        lease.status = "terminated"
        lease.terminated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()


def reap_expired(db: Session) -> int:
    """Reclaim leases whose inactivity window elapsed. Safe to call repeatedly."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale = db.scalars(
        select(models.WorkspaceLease).where(
            models.WorkspaceLease.status.in_(_ACTIVE),
            models.WorkspaceLease.expires_at.is_not(None),
            models.WorkspaceLease.expires_at < now,
        )
    ).all()
    for lease in stale:
        _terminate_row(db, lease, reason="expired")
    return len(stale)


def reconcile_on_startup(db: Session) -> int:
    """After a backend restart, any lease we believe is active either still has a
    live process (adopt it) or does not (mark terminated). Without this, restarts
    leak gym processes and hand out endpoints that answer nothing."""
    adopted = 0
    for lease in db.scalars(
        select(models.WorkspaceLease).where(models.WorkspaceLease.status.in_(_ACTIVE))
    ).all():
        if lease.endpoint and _provider().health(_handle_of(lease)):
            adopted += 1
        else:
            _terminate_row(db, lease, reason="restart-orphan")
    return adopted
