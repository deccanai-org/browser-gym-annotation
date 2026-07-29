"""Replay archived gym runs to recover the world trail they never recorded.

Reads nothing but the archive and the live gym; writes to the database only when
explicitly told to. Both commands replay — `audit` exists so the preflight
coverage numbers can be REFRESHED from a command rather than restated as a claim.

    # what the archive can reconstruct, per task — writes nothing
    .venv/bin/python -m scripts.backfill_trajectories audit \
        --archive "../../E Commerce Broswer Gym/trajectories/openai"

    # the same replay, showing exactly what WOULD be persisted
    .venv/bin/python -m scripts.backfill_trajectories backfill --archive DIR --task M40/bogus_pricematch

    # ...and again, persisting only the steps whose world matched the recording
    .venv/bin/python -m scripts.backfill_trajectories backfill --archive DIR --task M40/bogus_pricematch --write

Requires a running gym (`HARNESS_TOKEN` provisioned) and the live browser service.
`--accept snapshot` also keeps steps verified only against the recorded
cart/order/return counts — a real check, a coarser one; see app/backfill.py.

The deterministic clock needs no flag: `--tick auto` is the default and asks each
task's own seed world whether it schedules async events. `--tick on` / `--tick
off` force it, which is only useful for measuring one task against the other
answer — forcing it across a batch is wrong for whichever half does not match.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backfill  # noqa: E402 — the path insert above is what makes this importable
from app.config import settings  # noqa: E402


def _connect(args: argparse.Namespace):
    """The live pair: a gym for world state, a browser for actions. Separate
    processes on purpose — see live_browser/service.py."""
    gym = backfill.HarnessGym(args.gym, args.token)

    def executor(initial_url: str):
        return backfill.live_executor(initial_url, base_url=args.live)

    return gym, executor


def _runs(args: argparse.Namespace) -> list[backfill.ArchivedRun]:
    grouped = backfill.index(args.archive, tasks=args.tasks)
    picked = [run for paths in grouped.values() if (run := backfill.pick(paths)) is not None]
    picked.sort(key=lambda r: r.task_id)
    return picked[: args.limit] if args.limit else picked


def _seed_world(task_id: str, sessions) -> dict | None:
    """The world this task was captured against, when the database knows it.
    Replaying against a different task revision produces worlds that look right
    and are not, which no per-step check can catch."""
    if sessions is None:
        return None
    with contextlib.suppress(Exception):  # an unreachable DB must not stop an audit
        with sessions() as db:
            from sqlalchemy import select

            from app import models

            task = db.scalar(select(models.Task).where(models.Task.external_id == task_id))
            return (task.seed_state or {}).get("world") if task is not None else None
    return None


def _replay_all(args: argparse.Namespace, connect, sessions) -> list[backfill.Reconstruction]:
    gym, executor = connect(args)
    out: list[backfill.Reconstruction] = []
    for run in _runs(args):
        try:
            with executor(backfill.start_url(run, args.gym)) as ex:
                out.append(backfill.reconstruct(
                    run, gym, ex, accept=args.accept, tick=args.tick,
                    expected_seed_world=_seed_world(run.task_id, sessions),
                ))
        except Exception as exc:  # noqa: BLE001 — a browser that will not start is one
            # task's problem, and a 300-task audit that dies on task 12 reports
            # nothing at all about the other 288.
            out.append(backfill.Reconstruction(
                run=run, refused=f"could not drive a browser: {type(exc).__name__}: {exc}"
            ))
    return out


def _print_rows(rows: list[dict], summary: dict) -> None:
    for r in rows:
        flag = "ok  " if r["accepted"] == r["steps"] else ("part" if r["accepted"] else "none")
        note = f"  REFUSED {r['refused']}" if r["refused"] else ""
        # The clock is per task, so it belongs on the task's own line. A tick
        # column that only appeared in the summary would read as a batch-wide
        # setting, which is exactly the misreading that made --tick wrong before.
        clock = f" clock+{r['scheduledEvents']}" if r.get("ticked") else ""
        print(
            f"{flag} {r['taskId']:<48} world {r['accepted']}/{r['steps']}"
            f"  executed {r['executed']}/{r['steps']}"
            f"  [w{r['worldEvidence']} s{r['snapshotEvidence']} x{r['unreplayable']}]{clock}{note}"
        )
    print(f"\nreplayed {summary['tasks']} tasks: {json.dumps(summary)}")


def _audit(args: argparse.Namespace, connect, sessions) -> int:
    recons = _replay_all(args, connect, sessions)
    rows = [backfill.audit_row(r) for r in recons]
    summary = backfill.summarize(rows)
    if args.json:
        print(json.dumps({"tasks": rows, "summary": summary}, indent=2))
    else:
        _print_rows(rows, summary)
    return 0


def _backfill(args: argparse.Namespace, connect, sessions) -> int:
    recons = _replay_all(args, connect, sessions)
    rows = [backfill.audit_row(r) for r in recons]
    summary = backfill.summarize(rows)
    writes: list[dict] = []

    for recon in recons:
        if not args.write:
            continue
        # One transaction per task, committed as it finishes: a 300-task backfill
        # that dies on task 200 must keep the 199 it verified.
        with sessions() as db:
            try:
                report = backfill.apply(db, recon)
                db.commit()
            except Exception as exc:  # noqa: BLE001 — one bad task must not end the batch
                db.rollback()
                report = backfill.WriteReport(task_id=recon.run.task_id, refused=type(exc).__name__)
        writes.append(vars(report))

    if args.json:
        print(json.dumps({"tasks": rows, "summary": summary, "writes": writes, "wrote": args.write}, indent=2))
        return 0

    _print_rows(rows, summary)
    if not args.write:
        print("\nDRY RUN — nothing was written. Re-run with --write to persist the verified steps.")
        return 0
    for w in writes:
        print(
            f"wrote {w['task_id']:<48} checkpoints +{w['checkpoints_written']}"
            f" (reused {w['checkpoints_reused']})  steps {w['steps_updated']}"
            f"{'  IMPORTED' if w['imported'] else ''}"
            f"{'  conflicts ' + str(w['conflicts']) if w['conflicts'] else ''}"
            f"{'  refused: ' + w['refused'] if w['refused'] else ''}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_trajectories", description=__doc__.split("\n")[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("audit", "replay and report per-task coverage; never writes"),
        ("backfill", "replay and persist the verified steps (needs --write)"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--archive", required=True, help="a trajectories/ directory to scan")
        s.add_argument("--task", action="append", dest="tasks", help="task id (repeatable)")
        s.add_argument("--limit", type=int, help="cap the number of tasks replayed")
        s.add_argument("--accept", choices=[backfill.WORLD, backfill.SNAPSHOT], default=backfill.WORLD,
                       help="strength of evidence a step must carry to be kept (default: world)")
        s.add_argument("--tick", choices=backfill.TICK_MODES, default=backfill.TICK_AUTO,
                       help="deterministic clock: auto ticks only the tasks whose seed world "
                            "schedules async events (default); on/off force it, for measuring")
        s.add_argument("--gym", default=settings.gym_url)
        s.add_argument("--token", default=settings.gym_harness_token)
        s.add_argument("--live", default=settings.live_browser_url)
        s.add_argument("--json", action="store_true")
        if name == "backfill":
            s.add_argument("--write", action="store_true",
                           help="persist the reconstruction; without it this is a dry run")
    return p


def main(argv: list[str] | None = None, *, connect=_connect, sessions=None) -> int:
    args = build_parser().parse_args(argv)
    if sessions is None:
        from app.db import SessionLocal

        sessions = SessionLocal
    return (_audit if args.cmd == "audit" else _backfill)(args, connect, sessions)


if __name__ == "__main__":  # pragma: no cover — operator entry point
    raise SystemExit(main())
