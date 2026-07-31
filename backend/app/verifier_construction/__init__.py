"""Agentic verifier construction: Discriminator + Orchestrator.

Verifiers are derived from task_prompt + fixed environment + seed_data /
dynamic_data — never from a golden trajectory — then adversarially validated
against initial/golden states and optional alt-path / shortcut rollouts before
human compatibility review.
"""

from app.verifier_construction.discriminator import (
    ACTION_SEQUENCE_PATTERN,
    ALLOWED_PREDICATE_KINDS,
    BRIDGED_ENVIRONMENT,
    CONFIDENCE_ESCALATION_THRESHOLD,
    CheckpointRejected,
    DEFAULT_DECOMPOSE_MODEL,
    Discriminator,
    ESCALATION_MODEL,
    VerifierAxis,
    VerifierCheckpoint,
    VerifierSuite,
    merge_seed_layers,
    split_seed_initial,
    write_verifiers,
    write_verifiers_from_seed_initial,
)
from app.verifier_construction.orchestrator import (
    AdversarialOutcome,
    Orchestrator,
    ValidationResult,
    validate_suite,
)
from app.verifier_construction.seed_io import (
    fetch_seed_world_from_db,
    fetch_seed_world_live,
    find_seed_snapshot_path,
    load_seed_from_disk,
    load_seed_golden,
    load_seed_initial,
    resolve_gym_repo_path,
    resolve_seed_snapshots_root,
)
from app.verifier_construction.suite_adapter import suite_to_platform

__all__ = [
    "ACTION_SEQUENCE_PATTERN",
    "ALLOWED_PREDICATE_KINDS",
    "BRIDGED_ENVIRONMENT",
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
    "fetch_seed_world_from_db",
    "fetch_seed_world_live",
    "find_seed_snapshot_path",
    "load_seed_from_disk",
    "load_seed_golden",
    "load_seed_initial",
    "merge_seed_layers",
    "resolve_gym_repo_path",
    "resolve_seed_snapshots_root",
    "split_seed_initial",
    "suite_to_platform",
    "validate_suite",
    "write_verifiers",
    "write_verifiers_from_seed_initial",
]
