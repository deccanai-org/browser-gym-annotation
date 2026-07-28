"""Adapt Discriminator VerifierSuite → platform suite IR.

Platform expects ``{id, level, assertion, code, check}``. Discriminator emits
``{id, axis, subgoal, assertion, predicate, required}``.

Axis → level mapping (Task Review chips):
  correctness → backend
  non_hacking → safety
  honesty     → semantic
  forbidden   → safety  (best-effort; see GUARDRAIL on veto)

FORBIDDEN predicates describe a *harmful* signature (true ⇒ veto). Platform
``verify.evaluate`` has no veto / hard-fail — see ``# GUARDRAIL`` in
``app/verify.py``. Adapted FORBIDDEN checks carry ``veto: true`` metadata and
the job result surfaces ``warnings`` so Task Review can banner the gap.
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

FORBIDDEN_VETO_WARNING = (
    "forbidden_veto_unsupported: platform verify.evaluate has no veto/hard-fail; "
    "FORBIDDEN checks are mapped best-effort to level=safety with veto=true metadata"
)


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
        # Metadata for a future additive veto in verify.evaluate — ignored today.
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
        if cp.axis == VerifierAxis.FORBIDDEN and FORBIDDEN_VETO_WARNING not in warnings:
            warnings.append(FORBIDDEN_VETO_WARNING)
    return platform, warnings
