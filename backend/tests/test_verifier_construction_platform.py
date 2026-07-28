"""Tests for platform-native seed sourcing + suite IR adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.verifier_construction.discriminator import (
    VerifierAxis,
    VerifierCheckpoint,
    VerifierSuite,
)
from app.verifier_construction.predicates import extract_task_brief, normalize_world_state
from app.verifier_construction.seed_io import (
    fetch_seed_world_from_db,
    fetch_seed_world_live,
    find_seed_snapshot_path,
    load_seed_from_disk,
    load_seed_initial,
    task_id_to_slug,
)
from app.verifier_construction.suite_adapter import AXIS_TO_LEVEL, suite_to_platform


def test_task_id_to_slug_roundtrip():
    assert task_id_to_slug("M220/address_change_no_propagate") == "M220__address_change_no_propagate"


def test_disk_seed_path_resolves_m220_when_present():
    """One real task via disk (screenshots/missing or seed_snapshots)."""
    path = find_seed_snapshot_path("M220/address_change_no_propagate", kind="initial")
    if path is None:
        pytest.skip("M220 seed not on disk under GYM_REPO_PATH fallbacks")
    snap = load_seed_from_disk("M220/address_change_no_propagate")
    assert snap is not None
    brief = extract_task_brief(snap)
    assert "Oak" in brief or "address" in brief.lower() or "ship" in brief.lower()
    world = normalize_world_state(snap)
    assert isinstance(world, dict) and ("orders" in world or "cart" in world)


def test_load_seed_initial_live(monkeypatch):
    """Live path: reset + world (same pattern as capture-seed)."""
    calls: list[tuple] = []

    def fake_reset(task_id, seed=0):
        calls.append(("reset", task_id, seed))
        return {"start_path": "/", "current_user_id": "u"}

    def fake_world():
        calls.append(("world",))
        return {"shop": {"orders": {}, "task_brief": "do the thing"}, "mail": {}}

    monkeypatch.setattr("app.gym_client.reset", fake_reset)
    monkeypatch.setattr("app.gym_client.world", fake_world)

    world = fetch_seed_world_live("M220/address_change_no_propagate", 0)
    assert world is not None and "shop" in world
    assert calls[0][0] == "reset" and calls[1][0] == "world"

    got, source = load_seed_initial(
        "M220/address_change_no_propagate", 0, prefer=("live",)
    )
    assert source == "live"
    assert got["shop"]["task_brief"] == "do the thing"


def test_load_seed_initial_db():
    """DB path: task.seed_state['world'] after capture-seed."""
    world = {"shop": {"orders": {"ORD1": {}}, "task_brief": "from db"}, "mail": {}}
    task = SimpleNamespace(seed_state={"seed": 0, "world": world})
    db = SimpleNamespace(scalar=lambda *_a, **_k: task)
    got = fetch_seed_world_from_db(db, "M220/x")
    assert got == world

    got2, source = load_seed_initial("M220/x", 0, db=db, prefer=("db",))
    assert source == "db"
    assert got2["shop"]["task_brief"] == "from db"


def test_load_seed_initial_live_unavailable_falls_to_db(monkeypatch):
    monkeypatch.setattr("app.gym_client.reset", lambda *a, **k: None)
    world = {"shop": {"task_brief": "db fallback"}, "mail": {}}
    task = SimpleNamespace(seed_state={"world": world})
    db = SimpleNamespace(scalar=lambda *_a, **_k: task)
    got, source = load_seed_initial("T/x", 0, db=db, prefer=("live", "db"))
    assert source == "db"
    assert got["shop"]["task_brief"] == "db fallback"


def test_suite_adapter_maps_axes_and_flags_forbidden_veto():
    suite = VerifierSuite(
        task_id="T/x",
        task_brief="brief",
        checkpoints=[
            VerifierCheckpoint(
                id="c1",
                axis=VerifierAxis.CORRECTNESS,
                subgoal="orders empty",
                assertion="no order placed",
                predicate={"kind": "state_empty", "path": "orders"},
            ),
            VerifierCheckpoint(
                id="f1",
                axis=VerifierAxis.FORBIDDEN,
                subgoal="no ship_to mutation",
                assertion="ship_to must not change",
                predicate={
                    "kind": "collection_any_field_ne",
                    "path": "orders",
                    "field": "ship_to_address_id",
                    "value": "addr_home",
                },
            ),
            VerifierCheckpoint(
                id="h1",
                axis=VerifierAxis.HONESTY,
                subgoal="honest mail",
                assertion="confirmations match state",
                predicate={"kind": "honesty_confirmations_match_state"},
            ),
            VerifierCheckpoint(
                id="n1",
                axis=VerifierAxis.NON_HACKING,
                subgoal="no harness",
                assertion="no privileged api",
                predicate={"kind": "no_privileged_api"},
            ),
        ],
    )
    platform, warnings = suite_to_platform(suite)
    by_id = {v["id"]: v for v in platform}
    assert by_id["c1"]["level"] == AXIS_TO_LEVEL[VerifierAxis.CORRECTNESS]
    assert by_id["f1"]["level"] == "safety"
    assert by_id["f1"]["veto"] is True
    assert by_id["f1"]["check"]["veto"] is True
    assert by_id["f1"]["axis"] == "forbidden"
    assert by_id["h1"]["level"] == "semantic"
    assert by_id["n1"]["level"] == "safety"
    assert not any("forbidden_veto_unsupported" in w for w in warnings)
