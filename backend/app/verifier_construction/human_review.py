"""Human compatibility review hook for generated verifier suites.

Scoped to approve / reject / edit — humans confirm checkpoints make sense
against the task brief and seed state. They do not author verifier logic from
scratch (AAMAS-style division of labor).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.verifier_construction.discriminator import (
    CheckpointRejected,
    VerifierCheckpoint,
    VerifierSuite,
    write_verifiers,
)
from app.verifier_construction.orchestrator import validate_suite
from app.verifier_construction.predicates import (
    extract_task_brief,
    load_seed_snapshot,
    mentions_action_sequence,
)


def _print_suite(suite: VerifierSuite) -> None:
    print(f"\nTask: {suite.task_id}")
    print(f"Brief: {suite.task_brief}")
    print(f"Source model: {suite.source_model}")
    if suite.detected_traps:
        print(f"Detected traps: {suite.detected_traps}")
        print(f"FORBIDDEN coverage path: {suite.forbidden_coverage_path}")
        if suite.incomplete_forbidden_coverage or suite.has_forbidden_coverage_gap():
            print(
                "\n*** MANDATORY REVIEW: incomplete_forbidden_coverage ***\n"
                "Detected traps are non-empty but no FORBIDDEN checkpoint exists.\n"
                "Do NOT approve until a harmful-signature FORBIDDEN checkpoint is added.\n"
                "Orchestrator will hard-reject this suite as-is.\n"
            )
    print("\nCheckpoints (compatibility review — do these match the task + seed?):")
    for i, cp in enumerate(suite.checkpoints, 1):
        print(f"  [{i}] axis={cp.axis.value} id={cp.id}")
        print(f"      subgoal:    {cp.subgoal}")
        print(f"      assertion:  {cp.assertion}")
        print(f"      predicate:  {json.dumps(cp.predicate)}")
    print()


def _edit_checkpoint(cp: VerifierCheckpoint) -> VerifierCheckpoint:
    print(f"\nEditing checkpoint {cp.id!r} (blank keeps current value).")
    print("Edit prose for compatibility only — predicates must remain state-field checks.")
    subgoal = input(f"  subgoal [{cp.subgoal}]: ").strip() or cp.subgoal
    assertion = input(f"  assertion [{cp.assertion}]: ").strip() or cp.assertion
    pred_raw = input(f"  predicate JSON [{json.dumps(cp.predicate)}]: ").strip()
    predicate = json.loads(pred_raw) if pred_raw else dict(cp.predicate)
    for text in (subgoal, assertion, cp.id):
        if mentions_action_sequence(text):
            raise CheckpointRejected(
                f"edit rejected — action-sequence terms are forbidden: {text!r}"
            )
    return VerifierCheckpoint(
        id=cp.id,
        axis=cp.axis,
        subgoal=subgoal,
        assertion=assertion,
        predicate=predicate,
        required=cp.required,
    )


def review_suite_interactive(
    suite: VerifierSuite,
    *,
    seed_initial: dict[str, Any] | None = None,
    seed_final: dict[str, Any] | None = None,
) -> tuple[str, VerifierSuite]:
    """CLI loop: approve / reject / edit.

    Returns ``(decision, suite)`` where decision is approve|reject.
    """
    _print_suite(suite)
    if seed_initial is not None and seed_final is not None:
        result = validate_suite(suite, seed_initial, seed_final)
        print(f"Orchestrator gate: {'ACCEPT' if result.accepted else 'REJECT'}")
        print(f"  {result.reason}")
        if result.revision_flags:
            for flag in result.revision_flags:
                print(f"  - {flag}")
        print()

    while True:
        choice = input("Compatibility decision [a]pprove / [r]eject / [e]dit: ").strip().lower()
        if choice in {"a", "approve", "y", "yes"}:
            # Gap is the source of truth — an edit that adds FORBIDDEN clears the block
            # even if incomplete_forbidden_coverage was set earlier.
            if suite.has_forbidden_coverage_gap():
                print(
                    "Cannot approve: incomplete_forbidden_coverage "
                    f"(traps={suite.detected_traps}). Add a FORBIDDEN checkpoint "
                    "via [e]dit, or [r]eject for revision."
                )
                continue
            return "approve", suite
        if choice in {"r", "reject", "n", "no"}:
            reason = input("Reject reason (optional): ").strip()
            if reason:
                print(f"Rejected: {reason}")
            return "reject", suite
        if choice in {"e", "edit"}:
            raw = input("Checkpoint number to edit: ").strip()
            try:
                idx = int(raw) - 1
                suite.checkpoints[idx] = _edit_checkpoint(suite.checkpoints[idx])
            except (ValueError, IndexError) as exc:
                print(f"Invalid selection: {exc}")
                continue
            except CheckpointRejected as exc:
                print(f"Edit blocked: {exc}")
                continue
            except json.JSONDecodeError as exc:
                print(f"Invalid predicate JSON: {exc}")
                continue
            _print_suite(suite)
            continue
        print("Enter a, r, or e.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Human compatibility review for Discriminator-generated verifiers. "
            "Approve / reject / edit — do not author from scratch."
        )
    )
    parser.add_argument(
        "--seed-initial",
        required=True,
        type=Path,
        help="Path to seed_snapshots/.../seedN_initial.json",
    )
    parser.add_argument(
        "--seed-final",
        type=Path,
        default=None,
        help="Optional seedN_final.json for orchestrator gate display",
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=None,
        help="Optional pre-generated suite JSON; otherwise Discriminator writes one",
    )
    parser.add_argument(
        "--brief",
        type=str,
        default=None,
        help="Override task brief (default: read from seed_initial)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the final suite JSON here after approve",
    )
    args = parser.parse_args(argv)

    seed_initial = load_seed_snapshot(args.seed_initial)
    brief = args.brief or extract_task_brief(seed_initial)

    if args.suite:
        raw = json.loads(args.suite.read_text(encoding="utf-8"))
        suite = VerifierSuite(
            task_id=str(raw.get("task_id") or seed_initial.get("task_id") or ""),
            task_brief=str(raw.get("task_brief") or brief),
            checkpoints=[VerifierCheckpoint.from_dict(c) for c in raw.get("checkpoints", [])],
            source=str(raw.get("source") or "loaded"),
            source_model=str(raw.get("source_model") or "loaded"),
            detected_traps=list(raw.get("detected_traps") or []),
            incomplete_forbidden_coverage=bool(raw.get("incomplete_forbidden_coverage")),
            forbidden_coverage_path=str(raw.get("forbidden_coverage_path") or "n/a"),
        )
    else:
        suite = write_verifiers(brief, seed_initial)

    seed_final = load_seed_snapshot(args.seed_final) if args.seed_final else None
    decision, suite = review_suite_interactive(
        suite, seed_initial=seed_initial, seed_final=seed_final
    )

    if decision == "approve" and args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(suite.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote approved suite → {args.out}")

    return 0 if decision == "approve" else 1


if __name__ == "__main__":
    sys.exit(main())
