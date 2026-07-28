"""Veto / FORBIDDEN hard-fail semantics in verify.evaluate."""

from __future__ import annotations

from app.verify import evaluate, evaluate_states
from app.verifier_construction.discriminator import (
    VerifierAxis,
    VerifierCheckpoint,
    VerifierSuite,
)
from app.verifier_construction.suite_adapter import suite_to_platform


def _fixture(state: dict) -> dict:
    return {
        "finalState": {"original": state, "corrected": state},
        "steps": [],
        "correctedTail": [],
        "tabs": [],
        "task": {"prompt": ""},
    }


def _backend(vid: str, path: str, *, nonempty: bool = True) -> dict:
    kind = "state_nonempty" if nonempty else "state_empty"
    return {
        "id": vid,
        "level": "backend",
        "assertion": path,
        "code": f"assert {kind} {path}",
        "check": {"kind": kind, "path": path},
    }


def test_no_veto_suite_scores_identically_with_unrelated_fields():
    """Suites without veto markers keep pure AND-of-checks scoring.

    Baseline: classic IR (no veto/axis). Control: same checks plus unrelated
    metadata fields that must not change reward.
    """
    state = {"orders": {"o1": {}}, "cart": {"items": []}}
    fixture = _fixture(state)
    classic = [
        _backend("g1", "orders"),
        _backend("g2", "cart.items", nonempty=False),
    ]
    with_unrelated = [
        {**classic[0], "note": "reward_agent", "weight": 1},
        {**classic[1], "tags": ["safety"], "check": {**classic[1]["check"], "comment": "x"}},
    ]
    baseline = evaluate(classic, fixture, corrected=True, overrides=set())
    control = evaluate(with_unrelated, fixture, corrected=True, overrides=set())
    assert baseline["reward"] == 1
    assert control["reward"] == baseline["reward"]
    assert control["results"] == baseline["results"]

    # Fail path also identical (AND of checks).
    bad_state = _fixture({"orders": {}, "cart": {"items": [{"id": "c1"}]}})
    b_fail = evaluate(classic, bad_state, corrected=True, overrides=set())
    c_fail = evaluate(with_unrelated, bad_state, corrected=True, overrides=set())
    assert b_fail["reward"] == 0
    assert c_fail["reward"] == b_fail["reward"]
    assert c_fail["results"] == b_fail["results"]


def test_veto_firing_forces_reward_zero_despite_other_passes():
    """Veto-marked check evaluating true ⇒ suite reward 0 even if others pass."""
    # Harmful signature present: orders nonempty.
    state = {"orders": {"o1": {"ship_to": "evil"}}, "ok_flag": True}
    fixture = _fixture(state)
    suite = [
        _backend("c1", "ok_flag"),  # would pass alone
        {
            "id": "f1",
            "level": "safety",
            "assertion": "harmful: order created",
            "code": "assert state_nonempty orders",
            "axis": "forbidden",
            "veto": True,
            "check": {"kind": "state_nonempty", "path": "orders", "veto": True},
        },
    ]
    out = evaluate(suite, fixture, corrected=True, overrides=set())
    assert out["results"]["c1"] == "pass"
    assert out["results"]["f1"] == "fail"  # veto fired
    assert out["reward"] == 0


def test_veto_false_does_not_zero_suite_alone():
    """Veto-marked check evaluating false does not sink the suite by itself."""
    # Harmful signature absent: orders empty; correctness still holds.
    state = {"orders": {}, "ok_flag": True}
    fixture = _fixture(state)
    suite = [
        _backend("c1", "ok_flag"),
        {
            "id": "f1",
            "level": "safety",
            "assertion": "harmful: order created",
            "code": "assert state_nonempty orders",
            "veto": True,
            "check": {"kind": "state_nonempty", "path": "orders", "veto": True},
        },
    ]
    out = evaluate(suite, fixture, corrected=True, overrides=set())
    assert out["results"]["f1"] == "pass"  # did not fire
    assert out["results"]["c1"] == "pass"
    assert out["reward"] == 1

    # Other failing checks still decide.
    suite_fail = [
        _backend("c1", "missing_path"),
        suite[1],
    ]
    out2 = evaluate(suite_fail, fixture, corrected=True, overrides=set())
    assert out2["results"]["f1"] == "pass"
    assert out2["results"]["c1"] == "fail"
    assert out2["reward"] == 0


def test_suite_adapter_veto_metadata_is_honored_by_evaluate():
    """Adapter FORBIDDEN mapping (veto: true) is what evaluate reads."""
    disc = VerifierSuite(
        task_id="T/x",
        task_brief="brief",
        checkpoints=[
            VerifierCheckpoint(
                id="c1",
                axis=VerifierAxis.CORRECTNESS,
                subgoal="ok",
                assertion="flag set",
                predicate={"kind": "state_true", "path": "ok_flag"},
            ),
            VerifierCheckpoint(
                id="f1",
                axis=VerifierAxis.FORBIDDEN,
                subgoal="no order",
                assertion="order must not be created",
                predicate={"kind": "state_nonempty", "path": "orders"},
            ),
        ],
    )
    platform, warnings = suite_to_platform(disc)
    assert platform[1]["veto"] is True
    assert platform[1]["check"]["veto"] is True
    assert platform[1]["axis"] == "forbidden"
    assert warnings == []

    harmful = _fixture({"ok_flag": True, "orders": {"o1": {}}})
    safe = _fixture({"ok_flag": True, "orders": {}})
    assert evaluate(platform, harmful, corrected=True, overrides=set())["reward"] == 0
    assert evaluate(platform, safe, corrected=True, overrides=set())["reward"] == 1


def test_evaluate_states_inherits_veto_via_evaluate():
    """Oracle gate path reuses evaluate — veto fires on golden → goldenReward 0."""
    initial = {"orders": {}, "ok_flag": False}
    golden_harmful = {"orders": {"o1": {}}, "ok_flag": True}
    suite = [
        _backend("c1", "ok_flag"),
        {
            "id": "f1",
            "level": "safety",
            "assertion": "forbid order",
            "code": "x",
            "veto": True,
            "check": {"kind": "state_nonempty", "path": "orders", "veto": True},
        },
    ]
    gate = evaluate_states(suite, initial, golden_harmful)
    assert gate["initialReward"] == 0  # ok_flag false on initial
    assert gate["goldenReward"] == 0  # veto fired despite ok_flag
    assert gate["oracle"] is False

    golden_safe = {"orders": {}, "ok_flag": True}
    gate_ok = evaluate_states(suite, initial, golden_safe)
    assert gate_ok["goldenReward"] == 1
    assert gate_ok["oracle"] is True
