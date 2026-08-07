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
    leak gym processes and hand out endpoints that answer nothing.

    Expired leases are reaped FIRST, and the ordering is the whole point. A
    healthy process behind an expired lease is not something to adopt — it is
    precisely what a leak looks like — and adopting it renewed the leak on every
    restart. `reap_expired` is otherwise only reached when an annotator hits
    their per-annotator cap, so on a host where nobody hits the cap nothing ever
    reclaimed anything: found a lease still marked `ready` ten days past its
    expiry, its container up the whole time, holding a gym's worth of memory.
    """
    reap_expired(db)
    db.flush()      # so the loop below cannot re-adopt what was just reclaimed
    adopted = 0
    for lease in db.scalars(
        select(models.WorkspaceLease).where(models.WorkspaceLease.status.in_(_ACTIVE))
    ).all():
        if lease.endpoint and _provider().health(_handle_of(lease)):
            adopted += 1
        else:
            _terminate_row(db, lease, reason="restart-orphan")
    return adopted


# --------------------------------------------------------------------------- seeding
# A workspace outlives the live browser attached to it. Reopening a pane must not
# re-seed a world the annotator has spent an hour building by hand — but skipping
# a reset is a much stronger act than performing one, so it happens only when two
# INDEPENDENT sources agree: our own durable record of what we seeded, and the
# gym's own answer about what it is holding. Either one silent → reset.


def mark_seeded(
    db: Session,
    lease: models.WorkspaceLease,
    *,
    task_key: str,
    seed: int,
    reset_result: dict | None,
    version_id: UUID | None = None,
) -> None:
    """Record what this workspace was seeded with. Call ONLY after the reset
    actually succeeded.

    `task_key` is the registry key we posted; `seeded_task_id` is the id the GYM
    ECHOED BACK, and they are not always the same string. `M295/..._armB` builds a
    world whose own task_id drops the `_armB` suffix, so comparing the gym's answer
    against the registry key reports a false mismatch and re-seeds forever. Store
    both: the key is what we send, the echo is what we later compare against.

    `version_id` binds the world to the branch whose prefix was rebuilt into it, so
    switching versions forces a rebuild rather than silently reusing the previous
    version's world (two versions of an attempt share a (task, seed)). None for a
    plain seed with no branch prefix.
    """
    lease.seeded_task_key = task_key
    lease.seeded_seed = int(seed)
    echoed = (reset_result or {}).get("task_id")
    lease.seeded_task_id = str(echoed) if echoed else task_key
    lease.seeded_version_id = version_id
    db.commit()


def clear_seed_mark(db: Session, lease: models.WorkspaceLease | None) -> None:
    """Forget what we think is in there. Anything that resets or reloads this
    attempt's gym behind the live pane's back must call this, or the pane will
    later vouch for a world it never seeded."""
    if lease is None:
        return
    lease.seeded_task_key = None
    lease.seeded_task_id = None
    lease.seeded_seed = None
    lease.seeded_version_id = None
    db.commit()


def holds_seeded_world(
    lease: models.WorkspaceLease | None,
    endpoint: GymEndpoint,
    *,
    task_key: str,
    seed: int,
    version_id: UUID | None = None,
) -> bool:
    """Is it safe to reuse this workspace's world instead of re-seeding it?

    Fails closed at every step. The cost of a wrong NO is one reseed; the cost of a
    wrong YES is an annotator recording steps against somebody else's world, which
    is the M46/M15 bug this reset was added to prevent in the first place.

    Two factors, both required:

    1. **Our record.** The lease must be `ready`, its endpoint must be the one we
       are about to drive (``acquire`` and ``endpoint_for`` each run their own
       query, so a concurrent provision can leave two active rows), and its seed
       marker must match this exact (task, seed).
    2. **The gym's own answer.** A live read of ``/_harness/state``, whose task_id
       must equal the id the gym echoed at seed time and whose seed must match.
       This is what catches a runtime that restarted underneath a still-valid
       lease — a healthy endpoint holding a world nobody seeded.

    Note on the probe: ``/_harness/state`` LAZILY BUILDS ``A1/buy_wireless_mouse``
    seed 0 on a virgin process rather than erroring, so the probe can itself
    construct a world. That is safe here, and deliberately so. If the attempt's
    task is not A1/seed-0 the fabricated world mismatches and we reset. If it IS
    A1/seed-0 we skip the reset — and the lazy path calls the same
    ``_reset_inline`` a real reset does, so the world is byte-identical to one we
    would have built (measured). Either way the annotator gets a correct world;
    the only loss is work that a restarted runtime had already destroyed.

    Deliberately NOT used as evidence:

    * ``/_harness/snapshot`` — omits ``seed``, so a seed-3 world reads as seed-0.
    * ``/_harness/verify`` — writes ``step`` AND permanently latches each
      milestone's ``fired_at_step``. Probing with it would corrupt the score of the
      very episode we are trying to preserve.
    * ``state["step"]`` as a has-been-driven signal — only ``load_state`` and
      ``verify`` ever assign it, so a world built entirely through the live browser
      reports step 0 forever.
    """
    if lease is None or lease.status != "ready" or not lease.endpoint:
        return False
    if lease.endpoint != endpoint.base_url:
        return False
    if lease.seeded_seed is None or lease.seeded_task_key != task_key:
        return False
    if int(lease.seeded_seed) != int(seed):
        return False
    # The world holds ONE version's branch prefix. Reusing it for a different
    # version would drive the new branch against the old branch's world — the
    # two-versions-one-world hazard the rebuild exists to remove.
    if lease.seeded_version_id != version_id:
        return False

    try:
        live = endpoint.state()
    except Exception:  # noqa: BLE001 — an unanswerable gym is an unusable witness
        return False
    if not isinstance(live, dict):
        return False
    if str(live.get("task_id") or "") != str(lease.seeded_task_id or ""):
        return False
    try:
        return int(live.get("seed")) == int(seed)
    except (TypeError, ValueError):
        return False
