"""Workspace isolation: lease lifecycle + the human/agent runtime split.

Uses a fake runtime provider so nothing real is spawned — the contract under test
is the lease bookkeeping, not uvicorn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import models
from app.config import settings
from app.workspace import manager
from app.workspace.provider import WorkspaceHandle


class FakeProvider:
    """Records what it was asked to do; `alive` controls health so a test can kill
    a workspace behind the manager's back."""

    kind = "local_process"

    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.terminated: list[str] = []
        self.alive: dict[str, bool] = {}
        self._n = 0

    def provision(self, *, label: str) -> WorkspaceHandle:
        self._n += 1
        ref = f"pid-{self._n}"
        self.provisioned.append(label)
        self.alive[ref] = True
        return WorkspaceHandle(
            endpoint=f"http://127.0.0.1:900{self._n}", external_ref=ref,
            runtime_kind=self.kind, image_digest="sha256:test",
        )

    def health(self, handle: WorkspaceHandle) -> bool:
        return self.alive.get(handle.external_ref, False)

    def terminate(self, handle: WorkspaceHandle) -> None:
        self.terminated.append(handle.external_ref)
        self.alive[handle.external_ref] = False


@pytest.fixture()
def iso(monkeypatch):
    """Isolation ON with a fake runtime."""
    fake = FakeProvider()
    monkeypatch.setattr(manager, "_provider", lambda: fake)
    monkeypatch.setattr(manager, "isolation_available", lambda: True)
    return fake


@pytest.fixture()
def attempt(db_session):
    task = models.Task(external_id=f"WS-{uuid4().hex[:8]}", title="t", prompt="p", source="gym")
    ann = models.Annotator(email=f"ws-{uuid4().hex[:8]}@test")
    db_session.add_all([task, ann])
    db_session.flush()
    s = models.ReviewSession(task_id=task.id, annotator_id=ann.id, source="gym")
    db_session.add(s)
    db_session.commit()
    return s


def test_isolation_off_by_default_falls_back_to_shared_gym(db_session, attempt):
    """Without isolation configured we must fall back to the SHARED gym — and the
    caller can tell, so two annotators are never silently put in one world."""
    assert manager.isolation_available() is False
    assert manager.acquire(db_session, attempt.id) is None
    assert manager.endpoint_for(db_session, attempt.id).base_url == settings.gym_url


def test_acquire_provisions_then_reuses_a_healthy_lease(db_session, attempt, iso):
    first = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    assert first is not None and first.status == "ready"
    assert first.purpose == manager.HUMAN
    assert first.endpoint and first.external_ref
    assert first.environment_image_digest == "sha256:test"

    again = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    assert again.id == first.id, "a healthy lease must be reused, not duplicated"
    assert len(iso.provisioned) == 1

    # and the attempt's gym calls are routed to ITS workspace, not the shared gym
    assert manager.endpoint_for(db_session, attempt.id).base_url == first.endpoint


def test_dead_workspace_is_reclaimed_and_replaced(db_session, attempt, iso):
    first = manager.acquire(db_session, attempt.id)
    iso.alive[first.external_ref] = False  # process died behind our back

    second = manager.acquire(db_session, attempt.id)
    assert second.id != first.id, "a dead lease must be replaced, not handed back"
    assert first.external_ref in iso.terminated, "the dead lease must be reclaimed"
    db_session.refresh(first)
    assert first.status == "terminated"


def test_human_and_agent_branch_workspaces_are_separate(db_session, attempt, iso):
    """The core isolation rule: an agent branch run gets its OWN runtime, so it can
    never reset the world out from under the human mid-review."""
    human = manager.acquire(db_session, attempt.id, purpose=manager.HUMAN)
    branch = manager.acquire(db_session, attempt.id, purpose=manager.AGENT_BRANCH)

    assert human.id != branch.id
    assert human.endpoint != branch.endpoint, "human and agent must not share a gym process"
    assert {human.purpose, branch.purpose} == {manager.HUMAN, manager.AGENT_BRANCH}
    # the live browser still resolves to the HUMAN workspace
    assert manager.endpoint_for(db_session, attempt.id).base_url == human.endpoint

    # tearing down the branch worker leaves the human workspace untouched
    manager.release(db_session, branch)
    db_session.refresh(human)
    assert human.status == "ready"
    assert manager.endpoint_for(db_session, attempt.id).base_url == human.endpoint


def test_touch_extends_the_inactivity_window(db_session, attempt, iso):
    lease = manager.acquire(db_session, attempt.id)
    lease.expires_at = datetime.utcnow() + timedelta(minutes=1)
    db_session.commit()
    before = lease.expires_at

    manager.touch(db_session, lease)
    assert lease.expires_at > before, "active work must not be reaped mid-annotation"


def test_reaper_reclaims_only_expired_leases(db_session, attempt, iso):
    live = manager.acquire(db_session, attempt.id, purpose=manager.HUMAN)
    doomed = manager.acquire(db_session, attempt.id, purpose=manager.AGENT_BRANCH)
    doomed.expires_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()

    assert manager.reap_expired(db_session) == 1
    db_session.refresh(doomed)
    db_session.refresh(live)
    assert doomed.status == "terminated" and doomed.external_ref in iso.terminated
    assert live.status == "ready", "an active lease must survive the reaper"


def test_restart_reconciliation_drops_orphans_and_adopts_live(db_session, attempt, iso):
    """After a restart, a lease either still has a live process (adopt) or does not
    (terminate) — otherwise we leak processes and serve dead endpoints."""
    alive = manager.acquire(db_session, attempt.id, purpose=manager.HUMAN)
    orphan = manager.acquire(db_session, attempt.id, purpose=manager.AGENT_BRANCH)
    iso.alive[orphan.external_ref] = False

    adopted = manager.reconcile_on_startup(db_session)
    assert adopted == 1
    db_session.refresh(alive)
    db_session.refresh(orphan)
    assert alive.status == "ready"
    assert orphan.status == "terminated"


def test_failed_provisioning_does_not_wedge_the_attempt(db_session, attempt, monkeypatch):
    class Broken(FakeProvider):
        def provision(self, *, label: str):
            raise RuntimeError("no port available")

    monkeypatch.setattr(manager, "_provider", lambda: Broken())
    monkeypatch.setattr(manager, "isolation_available", lambda: True)
    with pytest.raises(RuntimeError, match="workspace provisioning failed"):
        manager.acquire(db_session, attempt.id)

    # the failed lease is closed out, not left dangling as "provisioning"
    rows = db_session.scalars(
        select(models.WorkspaceLease).where(models.WorkspaceLease.attempt_id == attempt.id)
    ).all()
    assert rows and all(r.status == "terminated" for r in rows)


def test_agent_run_uses_its_own_workspace_and_never_the_humans(db_session, attempt, iso, monkeypatch, _session_factory):
    """THE isolation rule, end to end: a batch agent run must get its own
    short-lived branch workspace and release it, leaving the annotator's live
    workspace untouched. Running against the human's gym would reset the world
    out from under them mid-review."""
    from app.api import gym as gym_api

    human = manager.acquire(db_session, attempt.id, purpose=manager.HUMAN)
    human_endpoint = human.endpoint

    monkeypatch.setattr(gym_api.workspace, "isolation_available", lambda: True)
    # the job opens its OWN session in production; point that at the test DB
    monkeypatch.setattr(gym_api, "SessionLocal", _session_factory)

    used: list[str] = []
    with gym_api._agent_workspace(str(attempt.id)) as gym:
        used.append(gym.base_url)

    assert used and used[0] != human_endpoint, "the agent must not drive the human's workspace"

    db_session.expire_all()
    db_session.refresh(human)
    assert human.status == "ready", "the human workspace must survive the agent run"
    assert manager.endpoint_for(db_session, attempt.id).base_url == human_endpoint

    # the branch worker was reclaimed when the run finished
    branch = manager.active_lease(db_session, attempt.id, purpose=manager.AGENT_BRANCH)
    assert branch is None, "the branch worker must be torn down after the run"


def test_agent_workspace_falls_back_to_the_shared_gym_without_isolation(db_session, attempt):
    """Without isolation the behaviour must be exactly what it was — the module,
    same seams — so nothing silently changes for existing flows."""
    from app import gym_client
    from app.api import gym as gym_api

    with gym_api._agent_workspace(str(attempt.id)) as gym:
        assert gym is gym_client


# --------------------------------------------------------------------------- kill safety
def test_a_pid_that_is_not_our_gym_is_never_signalled(monkeypatch):
    """A lease row outlives the process it names and the OS recycles pids, so the
    number stored yesterday may belong to something else today. The reaper calls
    terminate() for exactly those rows — and it is reachable from a plain pytest
    run, because the startup reconciler walks every lease in whatever database is
    configured. An unverified kill is a signal sent to a stranger."""
    from app.workspace.provider import LocalProcessRuntimeProvider, WorkspaceHandle

    killed: list[int] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid))
    provider = LocalProcessRuntimeProvider()

    monkeypatch.setattr(provider, "_is_our_gym", staticmethod(lambda pid: False))
    provider.terminate(WorkspaceHandle(endpoint="", external_ref="4242", runtime_kind="local_process"))
    assert killed == [], "a pid we cannot prove is ours must be left alone"

    monkeypatch.setattr(provider, "_is_our_gym", staticmethod(lambda pid: True))
    provider.terminate(WorkspaceHandle(endpoint="", external_ref="4242", runtime_kind="local_process"))
    assert killed == [4242], "…and a verified one is still reclaimed"


def test_an_unreadable_process_answers_no(monkeypatch):
    """Refusing when we cannot tell is the whole point: a leaked gym costs memory
    and is recoverable, killing somebody else's process is not."""
    from app.workspace.provider import LocalProcessRuntimeProvider

    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
    assert LocalProcessRuntimeProvider._is_our_gym(4242) is False


def test_only_a_gym_command_line_counts(monkeypatch):
    from app.workspace.provider import LocalProcessRuntimeProvider

    class Out:
        def __init__(self, s): self.stdout = s

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Out("python -m uvicorn server.main:app --port 9001"))
    assert LocalProcessRuntimeProvider._is_our_gym(4242) is True
    monkeypatch.setattr("subprocess.run", lambda *a, **k: Out("/usr/bin/postgres -D /data"))
    assert LocalProcessRuntimeProvider._is_our_gym(4242) is False


# --------------------------------------------------------------------------- docker runtime
class FakeDocker:
    """Stands in for the docker CLI. `run` hands back a container id, `port` the
    published mapping — modelling what the real daemon returns."""

    def __init__(self, *, port=34567, running="true", healthy=True):
        self.calls: list[list[str]] = []
        self.port, self.running, self.healthy = port, running, healthy
        self.removed: list[str] = []

    def __call__(self, *args, timeout=120):
        self.calls.append(list(args))
        if args[0] == "version":
            return "27.0.0"
        if args[0] == "run":
            return "c0ffee1234567890"
        if args[0] == "port":
            return f"0.0.0.0:{self.port}"
        if args[0] == "inspect" and "{{.State.Running}}" in args:
            return self.running
        if args[0] == "image":
            return "sha256:deadbeef"
        if args[0] == "rm":
            self.removed.append(args[-1])
            return args[-1]
        return ""


def _docker_provider(monkeypatch, fake, healthy=True):
    from app.workspace import provider as prov

    p = prov.DockerRuntimeProvider(image="browser-gym:test")
    monkeypatch.setattr(p, "_docker", fake)
    monkeypatch.setattr(prov, "_harness_ok", lambda endpoint, timeout=2.0: healthy)
    return p


def test_a_workspace_container_is_reachable_through_the_host_gateway(monkeypatch):
    """Inside a container `localhost` is the container itself, so a workspace's
    published port is only reachable through the gateway. Pointing the backend at
    localhost would make every isolated workspace look dead."""
    from app.config import settings

    monkeypatch.setattr(settings, "docker_host_gateway", "host.docker.internal")
    fake = FakeDocker(port=34567)
    handle = _docker_provider(monkeypatch, fake).provision(label="attempt-1")

    assert handle.endpoint == "http://host.docker.internal:34567"
    assert handle.runtime_kind == "docker"
    assert handle.external_ref == "c0ffee1234567890"


def test_docker_picks_the_host_port_so_the_free_port_race_cannot_happen(monkeypatch):
    """The process provider retries around a free-port race it cannot win. Asking
    the daemon to publish 0:8000 removes the race instead of retrying it."""
    fake = FakeDocker()
    _docker_provider(monkeypatch, fake).provision(label="a")
    run = next(c for c in fake.calls if c[0] == "run")
    assert "0:8000" in run, "the daemon must assign the host port"


def test_the_harness_token_reaches_the_workspace(monkeypatch):
    """A workspace whose token does not match answers 401 to every harness call —
    it would look healthy at the port and dead at the API."""
    from app.config import settings

    monkeypatch.setattr(settings, "gym_harness_token", "tok-abc")
    fake = FakeDocker()
    _docker_provider(monkeypatch, fake).provision(label="a")
    run = next(c for c in fake.calls if c[0] == "run")
    assert "HARNESS_TOKEN=tok-abc" in run


def test_a_container_that_never_becomes_healthy_is_removed(monkeypatch):
    """Otherwise a failed provision leaks a container per attempt."""
    fake = FakeDocker(running="false")
    p = _docker_provider(monkeypatch, fake, healthy=False)
    with pytest.raises(RuntimeError, match="never became healthy|published no port"):
        p.provision(label="a")
    assert fake.removed == ["c0ffee1234567890"], "the dead container must be reclaimed"


def test_terminate_removes_the_container_by_id(monkeypatch):
    """A container id is never recycled, unlike a pid — so unlike the process
    provider this needs no proof-of-identity check before reclaiming."""
    from app.workspace.provider import WorkspaceHandle

    fake = FakeDocker()
    p = _docker_provider(monkeypatch, fake)
    p.terminate(WorkspaceHandle(endpoint="http://x", external_ref="abc123", runtime_kind="docker"))
    assert fake.removed == ["abc123"]


def test_isolation_stays_unavailable_without_a_daemon(monkeypatch):
    """Falling back to the shared gym is correct; handing out a dead endpoint is
    not. The caller must be able to tell the difference."""
    from app.workspace import provider as prov

    p = prov.DockerRuntimeProvider(image="browser-gym:test")
    monkeypatch.setattr(p, "_docker", lambda *a, timeout=120: None)
    assert p.available is False

    monkeypatch.setattr(p, "_docker", lambda *a, timeout=120: "27.0.0")
    assert p.available is True

    # …and with no image configured at all, which is the real deployment case:
    # an operator who has not built the gym image must fall back, not get a
    # provider that will fail on first provision.
    from app.config import settings

    monkeypatch.setattr(settings, "gym_image", "")
    assert prov.DockerRuntimeProvider().available is False, "no image, no isolation"


# --------------------------------------------------------------------------- capacity
def test_cap_refuses_rather_than_silently_sharing(iso, db_session, attempt, monkeypatch):
    """At the cap, acquire RAISES. It must not return None.

    None means "isolation unavailable, use the shared gym", and the caller acts on
    it by putting the annotator in the shared world — which is precisely the
    collision the cap exists to bound. The two outcomes have to stay distinct.
    """
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    ann_id = attempt.annotator_id
    manager.acquire(db_session, attempt.id, annotator_id=ann_id)

    other = models.ReviewSession(task_id=attempt.task_id, annotator_id=ann_id, source="gym")
    db_session.add(other)
    db_session.commit()

    with pytest.raises(manager.WorkspaceCapacityError):
        manager.acquire(db_session, other.id, annotator_id=ann_id)
    assert len(iso.provisioned) == 1


def test_cap_reaps_abandoned_work_before_refusing(iso, db_session, attempt, monkeypatch):
    """An expired lease held by the same annotator is reclaimed, not counted.

    The usual reason someone sits at the cap is an attempt they walked away from
    an hour ago. Refusing on that would be the cap punishing the wrong thing.
    """
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    ann_id = attempt.annotator_id
    stale = manager.acquire(db_session, attempt.id, annotator_id=ann_id)
    stale.expires_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()

    other = models.ReviewSession(task_id=attempt.task_id, annotator_id=ann_id, source="gym")
    db_session.add(other)
    db_session.commit()

    lease = manager.acquire(db_session, other.id, annotator_id=ann_id)
    assert lease is not None and lease.status == "ready"
    assert stale.external_ref in iso.terminated


def test_cap_is_per_annotator_not_global(iso, db_session, attempt, monkeypatch):
    """One annotator at the cap must not block anyone else."""
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)

    mate = models.Annotator(email=f"ws-{uuid4().hex[:8]}@test")
    db_session.add(mate)
    db_session.flush()
    theirs = models.ReviewSession(task_id=attempt.task_id, annotator_id=mate.id, source="gym")
    db_session.add(theirs)
    db_session.commit()

    assert manager.acquire(db_session, theirs.id, annotator_id=mate.id) is not None


def test_agent_branch_leases_count_against_the_cap(iso, db_session, attempt, monkeypatch):
    """A branch worker is a whole gym runtime too. Exempting it would let the cap
    be walked straight past by alternating purposes."""
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    ann_id = attempt.annotator_id
    manager.acquire(db_session, attempt.id, annotator_id=ann_id, purpose=manager.AGENT_BRANCH)
    with pytest.raises(manager.WorkspaceCapacityError):
        manager.acquire(db_session, attempt.id, annotator_id=ann_id, purpose=manager.HUMAN)


def test_reacquiring_the_same_attempt_does_not_consume_capacity(iso, db_session, attempt, monkeypatch):
    """Reopening a live browser reuses the lease. If that counted, the second open
    of a single attempt at cap=1 would refuse the annotator their own workspace."""
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    first = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    again = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    assert again is not None and again.id == first.id
    assert len(iso.provisioned) == 1


def test_no_annotator_means_no_cap(iso, db_session, attempt, monkeypatch):
    """Internal callers that pass no annotator (system replays) are not capped —
    they are bounded by their own concurrency and release in a finally."""
    monkeypatch.setattr(settings, "workspace_max_per_annotator", 1)
    assert manager.acquire(db_session, attempt.id, annotator_id=None) is not None


# --------------------------------------------------------------------------- seed marker
class FakeGym:
    """A gym endpoint that reports whatever we tell it to hold."""

    def __init__(self, base_url="http://127.0.0.1:9001", task_id=None, seed=None, raises=False, returns=None):
        self.base_url = base_url
        self._task_id, self._seed = task_id, seed
        self._raises, self._returns = raises, returns

    def state(self):
        if self._raises:
            raise OSError("gym did not answer")
        if self._returns is not None:
            return self._returns
        return {"task_id": self._task_id, "seed": self._seed, "step": 0}


def _seeded(db, lease, key="M37/false_overcharge", seed=0, echoed=None):
    manager.mark_seeded(db, lease, task_key=key, seed=seed,
                        reset_result={"task_id": echoed or key})
    return lease


def test_reuses_a_world_both_sources_agree_on(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is True


def test_never_seeded_lease_is_not_reusable(iso, db_session, attempt):
    """A fresh workspace has a world nobody built. Reuse would hand the annotator
    the gym's lazily-fabricated default task instead of theirs."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is False


def test_seed_zero_is_distinguishable_from_never_seeded(iso, db_session, attempt):
    """seed 0 is the common case. If the marker collapsed 'seeded with 0' into
    'no record', the feature would be off for almost every task."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    assert lease.seeded_seed is None
    _seeded(db_session, lease, seed=0)
    assert lease.seeded_seed == 0
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is True


def test_a_different_seed_is_a_different_world(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease, seed=0)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=3) is False


def test_a_different_task_is_a_different_world(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease, key="M37/false_overcharge")
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M46/sneaked_addon", seed=0) is False


def test_gym_holding_something_else_wins_over_our_record(iso, db_session, attempt):
    """The runtime restarted under a still-valid lease: our record says we seeded
    it, the gym says otherwise. The gym is the one holding the world."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    restarted = FakeGym(base_url=lease.endpoint, task_id="A1/buy_wireless_mouse", seed=0)
    assert manager.holds_seeded_world(lease, restarted, task_key="M37/false_overcharge", seed=0) is False


def test_an_unanswerable_gym_is_not_evidence(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    assert manager.holds_seeded_world(
        lease, FakeGym(base_url=lease.endpoint, raises=True), task_key="M37/false_overcharge", seed=0
    ) is False
    assert manager.holds_seeded_world(
        lease, FakeGym(base_url=lease.endpoint, returns="nonsense"), task_key="M37/false_overcharge", seed=0
    ) is False
    assert manager.holds_seeded_world(
        lease, FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=None),
        task_key="M37/false_overcharge", seed=0
    ) is False


def test_compares_the_id_the_gym_echoed_not_the_registry_key(iso, db_session, attempt):
    """M295/..._armB builds a world whose own task_id drops the _armB suffix.
    Comparing the gym's answer against the registry key would report a false
    mismatch and reseed that task's world on every single open, forever."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    key = "M295/injection_promo_forged_confirmation_armB"
    _seeded(db_session, lease, key=key, echoed="M295/injection_promo_forged_confirmation")
    gym = FakeGym(base_url=lease.endpoint, task_id="M295/injection_promo_forged_confirmation", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key=key, seed=0) is True


def test_a_lease_for_a_different_endpoint_is_not_evidence(iso, db_session, attempt):
    """acquire() and endpoint_for() each run their own query, so a concurrent
    provision can leave two active rows — the marker must not vouch for a runtime
    other than the one about to be driven."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    elsewhere = FakeGym(base_url="http://127.0.0.1:59999", task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, elsewhere, task_key="M37/false_overcharge", seed=0) is False


def test_a_terminated_lease_is_not_evidence(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    manager.release(db_session, lease)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is False


def test_no_lease_at_all_is_not_evidence(iso, db_session):
    """The shared-gym fallback. Whatever is in there may be someone else's."""
    gym = FakeGym(task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(None, gym, task_key="M37/false_overcharge", seed=0) is False


def test_clearing_the_mark_forces_a_reseed(iso, db_session, attempt):
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    _seeded(db_session, lease)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/false_overcharge", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is True
    manager.clear_seed_mark(db_session, lease)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/false_overcharge", seed=0) is False


def test_clearing_a_missing_lease_is_not_an_error(db_session):
    manager.clear_seed_mark(db_session, None)


# --------------------------------------------------------------------------- version-aware reuse
def test_switching_versions_forbids_reuse(iso, db_session, attempt):
    """Two versions of an attempt share a (task, seed). Reusing v1's world for v2
    would drive v2's branch against v1's world — the exact two-versions-one-world
    hazard the fork rebuild exists to remove."""
    from uuid import uuid4
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    v1, v2 = uuid4(), uuid4()
    manager.mark_seeded(db_session, lease, task_key="M37/x", seed=0,
                        reset_result={"task_id": "M37/x"}, version_id=v1)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/x", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/x", seed=0, version_id=v1) is True
    assert manager.holds_seeded_world(lease, gym, task_key="M37/x", seed=0, version_id=v2) is False


def test_a_plain_seed_carries_no_version(iso, db_session, attempt):
    """An unforked attempt seeds with version_id=None and must still be reusable
    for the same None on reopen."""
    lease = manager.acquire(db_session, attempt.id, annotator_id=attempt.annotator_id)
    manager.mark_seeded(db_session, lease, task_key="M37/x", seed=0,
                        reset_result={"task_id": "M37/x"}, version_id=None)
    gym = FakeGym(base_url=lease.endpoint, task_id="M37/x", seed=0)
    assert manager.holds_seeded_world(lease, gym, task_key="M37/x", seed=0, version_id=None) is True


def test_an_expired_lease_is_not_adopted_however_healthy_its_process_is(monkeypatch, db_session, attempt):
    """The leak: a lease still marked `ready` ten days past its expiry, container up
    the whole time.

    `reap_expired` is only reached when an annotator hits their per-annotator
    cap, so on a host where nobody hits the cap nothing ever reclaims anything —
    and `reconcile_on_startup` then ADOPTED the expired lease on every restart
    because its process was still answering. A healthy process behind an expired
    lease is not something to adopt; it is what a leak looks like.
    """
    monkeypatch.setattr(manager, "_provider", lambda: _AlwaysHealthy())
    past = datetime.utcnow() - timedelta(days=10)
    lease = models.WorkspaceLease(
        attempt_id=attempt.id, annotator_id=attempt.annotator_id, purpose="human",
        runtime_kind="docker", endpoint="http://host.docker.internal:55099",
        external_ref="c0ffee", status="ready", expires_at=past, last_active_at=past,
    )
    db_session.add(lease)
    db_session.commit()

    adopted = manager.reconcile_on_startup(db_session)
    db_session.commit()
    db_session.refresh(lease)

    assert lease.status == "terminated", "an expired lease must be reclaimed, not adopted"
    assert adopted == 0


def test_a_live_unexpired_lease_is_still_adopted(monkeypatch, db_session, attempt):
    """The reap must not evict work in progress — an annotator mid-task across a
    backend restart keeps their world."""
    monkeypatch.setattr(manager, "_provider", lambda: _AlwaysHealthy())
    future = datetime.utcnow() + timedelta(hours=1)
    lease = models.WorkspaceLease(
        attempt_id=attempt.id, annotator_id=attempt.annotator_id, purpose="human",
        runtime_kind="docker", endpoint="http://host.docker.internal:55098",
        external_ref="beef", status="ready", expires_at=future, last_active_at=datetime.utcnow(),
    )
    db_session.add(lease)
    db_session.commit()

    adopted = manager.reconcile_on_startup(db_session)
    db_session.commit()
    db_session.refresh(lease)

    assert lease.status == "ready"
    assert adopted == 1


class _AlwaysHealthy:
    """A provider whose processes are all up — the condition under which the
    expired lease used to be adopted."""

    def health(self, _handle):
        return True

    def terminate(self, _handle):
        return True
