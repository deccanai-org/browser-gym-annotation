"""Agentic verifier construction: Discriminator + Orchestrator.

Verifiers are derived from the task brief + seed-initial world only — never from
a golden trajectory — then adversarially validated against initial/golden states
and optional alt-path / shortcut rollouts before human compatibility review.
"""

from app.verifier_construction.discriminator import (
    ACTION_SEQUENCE_PATTERN,
    ALLOWED_PREDICATE_KINDS,
    CONFIDENCE_ESCALATION_THRESHOLD,
    CheckpointRejected,
    DEFAULT_DECOMPOSE_MODEL,
    Discriminator,
    ESCALATION_MODEL,
    VerifierAxis,
    VerifierCheckpoint,
    VerifierSuite,
    write_verifiers,
)
from app.verifier_construction.orchestrator import (
    AdversarialOutcome,
    Orchestrator,
    ValidationResult,
    validate_suite,
)

__all__ = [
    "ACTION_SEQUENCE_PATTERN",
    "ALLOWED_PREDICATE_KINDS",
    "CONFIDENCE_ESCALATION_THRESHOLD",
    "AdversarialOutcome",
    "CheckpointRejected",
    "DEFAULT_DECOMPOSE_MODEL",
    "Discriminator",
    "ESCALATION_MODEL",
    "Orchestrator",
    "ValidationResult",
    "VerifierAxis",
    "VerifierCheckpoint",
    "VerifierSuite",
    "validate_suite",
    "write_verifiers",
]
