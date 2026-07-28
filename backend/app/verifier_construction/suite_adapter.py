"""Adapt Discriminator VerifierSuite → platform suite IR.

Platform expects ``{id, level, assertion, code, check}``. Discriminator emits
``{id, axis, subgoal, assertion, predicate, required}``.

Axis → level mapping (Task Review chips):
  correctness → backend
  non_hacking → safety
  honesty     → semantic
  forbidden   → safety  (+ ``veto: true`` for ``verify.evaluate``)

FORBIDDEN predicates describe a *harmful* signature (true ⇒ veto). Adapted
FORBIDDEN checks carry ``veto: true`` on the verifier and nested ``check`` so
``verify.evaluate`` / ``evaluate_states`` hard-fail when the predicate fires.
"""

from __future__ import annotations

from typing import Any

from app.verifier_construction.discriminator import VerifierAxis, VerifierCheckpoint, VerifierSuite

AXIS_TO_LEVEL: dict[VerifierAxis, str] = {
    VerifierAxis.CORRECTNESS: "backend",
    VerifierAxis.NON_HACKING: "safety",
    VerifierAxis.HONESTY: "semantic",
    VerifierAxis.FORBIDDEN: "safety",
}


def checkpoint_to_platform(cp: VerifierCheckpoint) -> dict[str, Any]:
    """Map one Discriminator checkpoint to platform verifier IR."""
    level = AXIS_TO_LEVEL.get(cp.axis, "backend")
    predicate = dict(cp.predicate or {})
    kind = predicate.get("kind", "check")
    path = predicate.get("path", "")
    code = f"assert {kind} {path}".strip()
    out: dict[str, Any] = {
        "id": cp.id,
        "level": level,
        "assertion": cp.assertion or cp.subgoal,
        "code": code,
        "check": predicate,
        "axis": cp.axis.value,
        "subgoal": cp.subgoal,
        "required": cp.required,
    }
    if cp.axis == VerifierAxis.FORBIDDEN:
        # ``verify.evaluate`` reads veto / axis=forbidden and hard-fails when true.
        out["veto"] = True
        out["check"] = {**predicate, "veto": True}
    return out


def suite_to_platform(suite: VerifierSuite) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert a full Discriminator suite to platform IR.

    Returns ``(platform_suite, warnings)``.
    """
    warnings: list[str] = []
    platform: list[dict[str, Any]] = []
    for cp in suite.checkpoints:
        platform.append(checkpoint_to_platform(cp))
    return platform, warnings
