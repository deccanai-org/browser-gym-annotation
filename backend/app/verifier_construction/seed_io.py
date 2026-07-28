"""Platform-native seed sourcing for verifier construction.

Discriminator / Orchestrator accept bare worlds via ``normalize_world_state``;
this module only locates those worlds — live gym, persisted DB, or on-disk
snapshots under ``GYM_REPO_PATH`` / ``settings.gym_repo_path``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from app.verifier_construction.predicates import load_seed_snapshot

SeedSource = Literal["live", "db", "disk"]

# Last-resort disk roots so offline / LLM tests still find seeds when the
# platform env is unset (do not require ecommerce-browser-gym as a hard dep).
_FALLBACK_GYM_ROOTS = (
    Path("/Users/maroonferrari/Deccan/ecommerce-browser-gym"),
)


def resolve_gym_repo_path() -> Path | None:
    """Resolve the gym checkout: env ``GYM_REPO_PATH``, then settings, then fallbacks."""
    env = (os.environ.get("GYM_REPO_PATH") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    try:
        from app.config import settings

        cfg = (settings.gym_repo_path or "").strip()
        if cfg:
            p = Path(cfg).expanduser().resolve()
            if p.is_dir():
                return p
    except Exception:
        pass
    for root in _FALLBACK_GYM_ROOTS:
        if root.is_dir():
            return root.resolve()
    return None


def task_id_to_slug(task_id: str) -> str:
    """``M220/address_change_no_propagate`` → ``M220__address_change_no_propagate``."""
    return task_id.strip().replace("/", "__")


def slug_to_task_id(slug: str) -> str:
    """Inverse of :func:`task_id_to_slug` (first ``__`` only)."""
    s = slug.strip()
    if "__" in s:
        head, tail = s.split("__", 1)
        return f"{head}/{tail}"
    return s


def resolve_seed_snapshots_root(gym_repo: Path | None = None) -> Path | None:
    """Prefer ``{gym_repo}/seed_snapshots`` when it exists and is non-empty."""
    root = gym_repo or resolve_gym_repo_path()
    if root is None:
        return None
    snap = root / "seed_snapshots"
    if snap.is_dir():
        return snap
    return None


def _candidate_seed_paths(task_id_or_slug: str, *, kind: str, seed: int = 0) -> list[Path]:
    """Ordered candidate paths for ``seed{N}_{initial|final}.json``."""
    slug = task_id_to_slug(task_id_or_slug)
    fname = f"seed{seed}_{kind}.json"
    out: list[Path] = []
    gym = resolve_gym_repo_path()
    if gym is not None:
        out.append(gym / "seed_snapshots" / slug / fname)
        # Captured under screenshots/missing during gym QA — same JSON shape.
        out.append(gym / "screenshots" / "missing" / slug / "seed_state" / fname)
    for root in _FALLBACK_GYM_ROOTS:
        if gym is not None and root.resolve() == gym.resolve():
            continue
        out.append(root / "seed_snapshots" / slug / fname)
        out.append(root / "screenshots" / "missing" / slug / "seed_state" / fname)
    return out


def find_seed_snapshot_path(task_id_or_slug: str, *, kind: str = "initial", seed: int = 0) -> Path | None:
    """Return the first existing on-disk seed snapshot path, or None."""
    for path in _candidate_seed_paths(task_id_or_slug, kind=kind, seed=seed):
        if path.is_file():
            return path
    return None


def load_seed_from_disk(task_id_or_slug: str, *, kind: str = "initial", seed: int = 0) -> dict[str, Any] | None:
    """Load a seed snapshot from disk if present."""
    path = find_seed_snapshot_path(task_id_or_slug, kind=kind, seed=seed)
    if path is None:
        return None
    return load_seed_snapshot(path)


def fetch_seed_world_live(task_id: str, seed: int = 0) -> dict[str, Any] | None:
    """Live path: ``gym_client.reset(task_id, seed)`` + ``gym_client.world()``.

    Same pattern as capture-seed. Returns the bare multi-app world, or None when
    the gym is unreachable / task unknown.
    """
    from app import gym_client

    if gym_client.reset(task_id, seed) is None:
        return None
    world = gym_client.world()
    return world if isinstance(world, dict) else None


def fetch_seed_world_from_db(db: Any, task_id: str) -> dict[str, Any] | None:
    """DB path: read persisted ``task.seed_state["world"]`` after capture-seed."""
    from sqlalchemy import select

    from app import models

    task = db.scalar(select(models.Task).where(models.Task.external_id == task_id))
    if task is None:
        return None
    seed_state = task.seed_state or {}
    world = seed_state.get("world")
    return world if isinstance(world, dict) and world else None


def load_seed_initial(
    task_id: str,
    seed: int = 0,
    *,
    db: Any | None = None,
    prefer: tuple[SeedSource, ...] = ("live", "db", "disk"),
) -> tuple[dict[str, Any], SeedSource]:
    """Resolve an initial world for Discriminator write.

    Tries sources in ``prefer`` order. Raises ``FileNotFoundError`` if none work.
    """
    errors: list[str] = []
    for source in prefer:
        if source == "live":
            world = fetch_seed_world_live(task_id, seed)
            if world is not None:
                return world, "live"
            errors.append("live: gym unreachable or unknown task")
        elif source == "db":
            if db is None:
                errors.append("db: no session provided")
                continue
            world = fetch_seed_world_from_db(db, task_id)
            if world is not None:
                return world, "db"
            errors.append("db: no task.seed_state['world']")
        elif source == "disk":
            snap = load_seed_from_disk(task_id, kind="initial", seed=seed)
            if snap is not None:
                return snap, "disk"
            errors.append("disk: no seed snapshot found")
    raise FileNotFoundError(
        f"could not resolve seed_initial for {task_id!r} (seed={seed}): " + "; ".join(errors)
    )


def load_seed_golden(
    task_id: str,
    seed: int = 0,
    *,
    prefer: tuple[Literal["oracle", "disk"], ...] = ("oracle", "disk"),
    oracle_world: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Resolve a golden / final world for Orchestrator validate.

    ``oracle_world`` is the post-oracle ``gym_client.world()`` when the caller
    already ran the oracle (mirrors ``_autogen_verifiers_job``). Disk falls back
    to ``seed{N}_final.json``.
    """
    errors: list[str] = []
    for source in prefer:
        if source == "oracle":
            if isinstance(oracle_world, dict) and oracle_world:
                return oracle_world, "oracle"
            errors.append("oracle: no post-oracle world provided")
        elif source == "disk":
            snap = load_seed_from_disk(task_id, kind="final", seed=seed)
            if snap is not None:
                return snap, "disk"
            errors.append("disk: no seed_final snapshot found")
    raise FileNotFoundError(
        f"could not resolve seed_golden for {task_id!r} (seed={seed}): " + "; ".join(errors)
    )
