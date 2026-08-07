"""Which world an attempt actually owns.

There are two ways an attempt gets a world, and until this module existed there
was no way to ask which one you were in:

  * **workspace** — the attempt leased its own gym process (`WorkspaceLease`).
  * **bridged**   — the attempt leased a gym out of the bridge pool, and the five
                    realistic mocks are wired to it, so a click in ShopGym runs
                    through the real engine and re-projects into all five apps.

The bug this closes: a bridged (cua-hub) attempt never calls `workspace.acquire`,
so it has no lease — and `workspace.endpoint_for` falls through to
`GymEndpoint(settings.gym_url)`, the SHARED gym. Every caller that reached for a
world that way (`commit_actions`, `finalize_attempt`, `reset_live_world`) was
therefore reading, checkpointing and mutating **somebody else's world**, silently
and with no error anywhere. Route those through `world_for()` instead.

A bridged world is still a real gym at a real URL — the bridge tells us which one
when it leases it — so `BridgedWorld` is a thin wrapper over `GymEndpoint` plus
the bridge's own milestone verdict, rather than a second implementation of the
gym protocol.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app import bridge_client, workspace
from app.gym_client import GymEndpoint


class WorldPort(Protocol):
    """The slice of the gym protocol every world-reading caller actually uses."""

    def world(self) -> dict | None: ...
    def state(self) -> dict | None: ...
    def load_state(self, task_id: str, seed: int, state: dict, step: int | None = None) -> dict | None: ...
    def verify(self, step: int = 0) -> dict | None: ...
    def tick(self, step: int = 0) -> dict | None: ...
    def reset(self, task_id: str, seed: int = 0) -> dict | None: ...


def _pool_token() -> str | None:
    """Harness token of the bridged gym pool.

    The pool is started by the gym repo's own script, so it may run with a
    different HARNESS_TOKEN than the annotator's. Carrying it explicitly turns a
    silent 401-on-every-world-read into a config value.
    """
    return os.environ.get("CUA_BRIDGE_GYM_TOKEN") or None


def backend_visible(gym_url: str) -> str:
    """A pooled gym's URL as THIS process can reach it.

    The bridge reports the gym it leased in its OWN namespace — typically
    `http://127.0.0.1:8077`, because the bridge and its pool share a host. Inside
    a container that address is the container itself, so every world read fails
    and the attempt silently ends up with no initial checkpoint and no gate.

    The bridge and its pool are co-located, so the host we already use to reach
    the bridge is the host that reaches its gyms; only the PORT differs, and the
    port is what identifies the instance.
    """
    if not gym_url:
        return gym_url
    bridge_host = urlsplit(bridge_client.base_url()).hostname
    parts = urlsplit(gym_url)
    if not bridge_host or not parts.hostname or parts.hostname == bridge_host:
        return gym_url
    netloc = bridge_host if parts.port is None else f"{bridge_host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class BridgedWorld:
    """The gym the bridge leased for this attempt, plus the bridge's verdict.

    `session_id` is the bridge session (== the attempt id), which is what
    `/bridge/{sid}/verify` and `/bridge/{sid}/state` are keyed on.
    """

    __slots__ = ("gym", "session_id")

    kind = "bridged"

    def __init__(self, gym_url: str, session_id: str) -> None:
        self.gym = GymEndpoint(backend_visible(gym_url), token=_pool_token())
        self.session_id = session_id

    def __getattr__(self, name: str):
        """Delegate anything we don't override to the wrapped gym.

        Delegation rather than a re-declared surface, because callers duck-type:
        `replay.advance_clock` does `getattr(gym, "verify", None)` and skips the
        clock entirely when it is absent. A wrapper that *declares* every method
        would make those probes succeed and then fail at call time against a gym
        that never had it.
        """
        return getattr(object.__getattribute__(self, "gym"), name)

    def app_states(self) -> dict[str, dict]:
        """Per-app projected state — the mocks' own view, for `ui`-level checks."""
        try:
            return bridge_client.state(self.session_id)
        except bridge_client.BridgeError:
            return {}

    def verify(self, step: int = 0) -> dict | None:
        """The gym's REAL milestone verdict, taken through the bridge.

        Going through the bridge (rather than the gym directly) keeps the mocks'
        projection in step with the world the verdict was computed on.
        """
        try:
            return bridge_client.verify(self.session_id)
        except bridge_client.BridgeError:
            return self.gym.verify(step)


class UnleasedWorld:
    """A bridged attempt whose gym is NOT currently leased — it has no world.

    Two ways to get here: closing the pane releases the pooled gym (so somebody
    else can work), which clears `bridge_session_id`; and the bridge reaps an
    idle session on its own TTL, which clears nothing here but hands the gym to
    the next annotator all the same. Without this port, `world_for` fell through
    to `WorkspaceWorld(GymEndpoint(settings.gym_url))` — the SHARED gym — and
    finalize / certify / reset-world proceeded to read and *mutate* it. With one
    annotator that was invisible; with several it is the cross-contamination
    vector, and it fires on the most ordinary action in the product: leaving a
    task.

    "No world" is the honest answer, so every read returns None rather than
    somebody else's data. Callers that only observe (materialize) already degrade
    correctly on None; callers that would MUTATE must refuse — see the `kind`
    check in api/versions.py and api/live.py.
    """

    __slots__ = ()

    kind = "unleased"
    base_url = ""

    def world(self) -> dict | None:
        return None

    def state(self) -> dict | None:
        return None

    def app_states(self) -> dict[str, dict]:
        return {}

    def load_state(self, task_id: str, seed: int, state: dict, step: int | None = None) -> dict | None:
        return None

    def verify(self, step: int = 0) -> dict | None:
        return None

    def tick(self, step: int = 0) -> dict | None:
        return None

    def reset(self, task_id: str, seed: int = 0) -> dict | None:
        return None


class WorkspaceWorld:
    """The attempt's own leased gym process — the pre-existing isolation path."""

    __slots__ = ("gym",)

    kind = "workspace"

    def __init__(self, endpoint: GymEndpoint) -> None:
        self.gym = endpoint

    def __getattr__(self, name: str):
        """Pure delegation — see BridgedWorld.__getattr__. This port must be
        indistinguishable from the endpoint it wraps, including which methods it
        does NOT have."""
        return getattr(object.__getattribute__(self, "gym"), name)

    def app_states(self) -> dict[str, dict]:
        return {}


def bridged_gym_url(session) -> str:
    """The gym URL the bridge leased for this attempt, as persisted at start."""
    return (getattr(session, "bridge_gym_url", "") or "").strip()


def is_bridged(session) -> bool:
    return bool(getattr(session, "bridge_session_id", "") and bridged_gym_url(session))


def current_lease_url(session) -> str:
    """The gym this attempt holds RIGHT NOW, per the bridge's own lease table.

    `bridge_session_id` only records that we leased a gym once. The bridge reaps
    an idle session on its TTL and hands that gym to whoever opens next, and
    nothing tells us — so an attempt whose annotator walked away still resolved
    to it, and finalize / certify checkpointed a stranger's world as this
    attempt's final state, reset wiped their work in progress, and verify scored
    them instead. The lease is the authority; no lease means no world.

    A bridge we cannot reach is NOT evidence the lease is gone — nobody can lease
    anything while it is down — so that case keeps the URL we persisted rather
    than turning a blip into a refused finalize.
    """
    persisted = bridged_gym_url(session)
    try:
        leases = (bridge_client.pool_status() or {}).get("sessions") or {}
    except bridge_client.BridgeError:
        return persisted
    lease = leases.get(str(getattr(session, "bridge_session_id", "") or ""))
    if not isinstance(lease, dict):
        return ""
    # The bridge's answer wins over ours: if this session sits on a different
    # instance than the one we recorded, the recorded one is somebody else's now.
    return str(lease.get("gym") or "") or persisted


def owns_bridged_world(session) -> bool:
    """Does this attempt's world live in the bridged gym pool at all?

    Keyed on `cua_apps` — the five attempt SIDs, persisted when the attempt first
    opened — because that is a DURABLE fact about where this attempt's world
    lives, and it stays true while the gym is released. Deliberately NOT keyed on
    `cua_hub.enabled()` / `bridge_client.enabled()`: those are environment flags,
    so keying on them would make an attempt's world resolve differently in tests
    than in production, which is exactly the class of bug this module exists to
    stop.
    """
    return bool(getattr(session, "cua_apps", None))


def world_for(db: Session, session) -> WorldPort:
    """The world THIS attempt owns — never the shared gym by accident.

    Three cases, in order:
      * bridged and leased  → the gym the bridge leased for it
      * bridged, not leased → no world at all (the pane is closed, or the bridge
                              reaped an idle session; either way the gym went
                              back to the pool and now belongs to someone else)
      * everything else     → the existing workspace behaviour (its own lease,
                              else the shared gym, which is correct for a
                              non-bridged attempt)

    "Leased" is what the BRIDGE says, not what our row remembers — see
    `current_lease_url`.
    """
    if is_bridged(session):
        gym_url = current_lease_url(session)
        if gym_url:
            return BridgedWorld(gym_url, str(session.bridge_session_id))
        return UnleasedWorld()
    if owns_bridged_world(session):
        return UnleasedWorld()
    attempt_id: UUID | None = getattr(session, "id", None)
    return WorkspaceWorld(workspace.endpoint_for(db, attempt_id))
