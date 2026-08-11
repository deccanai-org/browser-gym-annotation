"""Agentic verifier construction: Discriminator + Orchestrator.

Verifiers are derived from task_prompt + fixed environment + seed_data /
dynamic_data — never from a golden trajectory — then adversarially validated
against initial/golden states and optional alt-path / shortcut rollouts before
human compatibility review.
"""

from app.verifier_construction.content_classifier import (
    DEFAULT_CLASSIFIER_MODEL,
    get_classifier_usage,
    reset_classifier_usage,
    set_content_classifier_fn,
)
from app.verifier_construction.content_regen import (
    CLASSIFIER_CONTENT_KINDS,
    CONTENT_MATCH_KINDS,
    MAX_CONTENT_REGEN_ATTEMPTS,
    ConstructionResult,
    content_failures_against_golden,
    is_classifier_content_predicate,
    is_content_match_predicate,
    write_and_validate_suite,
)
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
from app.verifier_construction.predicates import (
    MINIMAL_DIFF_INITIAL_KEY,
    attach_minimal_diff_initial,
    minimal_state_diff_unrelated,
)
from app.verifier_construction.suite_adapter import suite_to_platform

__all__ = [
    "MINIMAL_DIFF_INITIAL_KEY",
    "attach_minimal_diff_initial",
    "minimal_state_diff_unrelated",

    "ACTION_SEQUENCE_PATTERN",
    "ALLOWED_PREDICATE_KINDS",
    "BRIDGED_ENVIRONMENT",
    "CLASSIFIER_CONTENT_KINDS",
    "CONFIDENCE_ESCALATION_THRESHOLD",
    "CONTENT_MATCH_KINDS",
    "DEFAULT_CLASSIFIER_MODEL",
    "MAX_CONTENT_REGEN_ATTEMPTS",
    "AdversarialOutcome",
    "CheckpointRejected",
    "ConstructionResult",
    "DEFAULT_DECOMPOSE_MODEL",
    "Discriminator",
    "ESCALATION_MODEL",
    "Orchestrator",
    "ValidationResult",
    "VerifierAxis",
    "VerifierCheckpoint",
    "VerifierSuite",
    "content_failures_against_golden",
    "fetch_seed_world_from_db",
    "fetch_seed_world_live",
    "find_seed_snapshot_path",
    "get_classifier_usage",
    "is_classifier_content_predicate",
    "is_content_match_predicate",
    "load_seed_from_disk",
    "load_seed_golden",
    "load_seed_initial",
    "merge_seed_layers",
    "reset_classifier_usage",
    "resolve_gym_repo_path",
    "resolve_seed_snapshots_root",
    "set_content_classifier_fn",
    "split_seed_initial",
    "suite_to_platform",
    "validate_suite",
    "write_and_validate_suite",
    "write_verifiers",
    "write_verifiers_from_seed_initial",
]
