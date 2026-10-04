"""Command-line driver for the agent workflow.

A thin shell over `app.agents.runner` — it holds no business logic of its own, so
what it demonstrates is genuinely what the pipeline does rather than a parallel
happy path. The REST API will call the same two functions.

Usage
-----
    python -m app.cli "I need a cardiology appointment next week"
    python -m app.cli --patient 2 "my knee has been hurting since I fell"
    python -m app.cli --list                 # patients and recent runs
    python -m app.cli --pending              # escalations awaiting staff review
    python -m app.cli --approve 3            # resume a suspended run
    python -m app.cli --reject 3
    python -m app.cli --show 3               # what happened on one run

Agent logs are suppressed by default so the patient-facing result is readable;
pass --verbose to see the tool calls and audit writes as they happen.
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog
from sqlalchemy.orm import Session

from app.db.models import EscalationStatus
from app.db.session import SessionLocal
from app.services import escalation_service, patient_service, workflow_service

RULE = "-" * 72


def _quiet_logs() -> None:
    """Silence the structured agent logs.

    They are the right default for a server and the wrong one for a CLI, where they
    bury the one line the patient actually cares about.

    `logging.disable()` is not enough on its own: the app configures structlog with
    `PrintLoggerFactory`, which writes straight to stdout and never reaches the stdlib
    logging module. The filtering bound logger has to be raised as well.
    """
    logging.disable(logging.WARNING)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL),
        cache_logger_on_first_use=False,
    )


def _print_result(result) -> None:
    print(f"\n{RULE}")
    if result.awaiting_human_review:
        print("  AWAITING STAFF REVIEW")
    else:
        print(f"  {result.status.value.upper().replace('_', ' ')}")
    print(RULE)
    print(f"\n{result.summary}\n")

    details = [f"run #{result.workflow_run_id}", f"step: {result.current_step.value}"]
    if result.appointment_id:
        details.append(f"appointment #{result.appointment_id}")
    if result.escalation_id:
        details.append(f"escalation #{result.escalation_id}")
    print("  " + "  |  ".join(details))

    if result.awaiting_human_review:
        print(
            f"\n  A staff member must decide before this continues:\n"
            f"    python -m app.cli --approve {result.workflow_run_id}\n"
            f"    python -m app.cli --reject  {result.workflow_run_id}"
        )
    print()


def cmd_submit(db: Session, *, patient_id: int, text: str) -> int:
    from app.agents.llm import LLMNotConfiguredError
    from app.agents.runner import start_workflow

    try:
        patient_service.get_profile(db, patient_id)
    except Exception:
        print(f"No patient with id {patient_id}. Run `python -m app.cli --list` to see them.")
        return 1

    print(f'\nSubmitting for patient #{patient_id}: "{text}"')
    print("Running the agent pipeline - this takes up to a minute...")
    try:
        result = start_workflow(db, patient_id=patient_id, request_text=text)
    except LLMNotConfiguredError as exc:
        print(f"\n{exc}")
        return 1

    _print_result(result)
    return 0


def _has_pending_interrupt(db: Session, run_id: int) -> bool:
    """Whether the graph is actually suspended at a checkpoint for this run.

    An escalation row and a suspended graph are two different things, and a run can
    have the first without the second: `flag_uncertain_routing` records an escalation
    and lets the graph finish, and the safety gate falls back to the same shape when
    the Safety agent errors. Either way there is no checkpoint left to resume from, so
    a decision would be a silent no-op — better to say so than to appear to work.
    """
    from app.agents.graph import build_graph, thread_config

    try:
        snapshot = build_graph(db).get_state(thread_config(run_id))
    except Exception:
        return False
    return any(task.interrupts for task in (getattr(snapshot, "tasks", ()) or ()))


def cmd_resume(db: Session, *, run_id: int, approved: bool) -> int:
    from app.agents.runner import resume_workflow

    try:
        run = workflow_service.get_run(db, run_id)
    except Exception:
        print(f"No workflow run with id {run_id}.")
        return 1

    pending = escalation_service.get_pending_for_workflow(db, run_id)
    if pending and not _has_pending_interrupt(db, run_id):
        print(
            f"\n{RULE}\n  CANNOT RESUME\n{RULE}\n\n"
            f"  Run #{run_id} has escalation #{pending.id} ({pending.reason.value}) pending,\n"
            f"  but the graph is not suspended at a checkpoint - it ran to completion.\n\n"
            f"  A decision here would do nothing, so it is not being sent. Inspect the\n"
            f"  run with `python -m app.cli --show {run_id}`; resolving this escalation\n"
            f"  needs staff to act outside the workflow.\n\n"
            f"  status: {run.status.value}  |  step: {run.current_step.value}\n"
        )
        return 1

    verb = "Approving" if approved else "Rejecting"
    print(f"\n{verb} run #{run_id} as staff...")
    _print_result(resume_workflow(db, workflow_run_id=run_id, approved=approved))
    return 0


def cmd_list(db: Session) -> int:
    print(f"\n{RULE}\n  PATIENTS\n{RULE}")
    rows = patient_service.list_patients(db)
    if not rows:
        print("  none - load sample data with `python -m seed.seed_data --reset`")
    for profile, user in rows:
        print(f"  #{profile.id:<4} {user.name:<24} {user.email}")

    runs = workflow_service.list_runs(db, limit=10)
    print(f"\n{RULE}\n  RECENT RUNS\n{RULE}")
    if not runs:
        print("  none yet")
    for run in runs:
        flag = "  <-- needs review" if run.status.value == "awaiting_review" else ""
        print(
            f"  #{run.id:<4} patient {run.patient_id:<3} {run.status.value:<16}"
            f"{run.current_step.value:<22} {run.raw_request[:40]}{flag}"
        )
    print()
    return 0


def cmd_pending(db: Session) -> int:
    pending = escalation_service.list_escalations(db, status=EscalationStatus.PENDING)
    print(f"\n{RULE}\n  ESCALATIONS AWAITING REVIEW\n{RULE}")
    if not pending:
        print("  none - nothing is waiting on staff\n")
        return 0
    for esc in pending:
        print(f"\n  escalation #{esc.id}  |  run #{esc.workflow_run_id}  |  {esc.reason.value}")
        print(f"    {esc.detail[:200]}")
        print(f"    python -m app.cli --approve {esc.workflow_run_id}")
    print()
    return 0


def cmd_show(db: Session, run_id: int) -> int:
    try:
        run = workflow_service.get_run(db, run_id)
    except Exception:
        print(f"No workflow run with id {run_id}.")
        return 1

    print(f"\n{RULE}\n  RUN #{run.id}\n{RULE}")
    print(f"  patient   : {run.patient_id}")
    print(f"  status    : {run.status.value}")
    print(f"  step      : {run.current_step.value}")
    print(f"  requested : {run.raw_request}")

    snapshot = run.state_snapshot or {}
    if snapshot:
        print("\n  state snapshot")
        for key, value in snapshot.items():
            print(f"    {key:<22} {str(value)[:90]}")

    escalations = escalation_service.list_for_workflow(db, run_id)
    if escalations:
        print("\n  escalations")
        for esc in escalations:
            print(f"    #{esc.id} {esc.reason.value:<28} {esc.status.value}")
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Drive the AgentCare workflow from the terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  python -m app.cli "I need a cardiology appointment next week"\n'
            '  python -m app.cli --patient 2 "my knee hurts"\n'
            '  python -m app.cli --pending\n'
            '  python -m app.cli --approve 3\n'
        ),
    )
    parser.add_argument("request", nargs="?", help="the patient's free-text request")
    parser.add_argument("-p", "--patient", type=int, default=1, help="patient id (default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show agent logs")

    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="list patients and recent runs")
    actions.add_argument("--pending", action="store_true", help="list escalations awaiting review")
    actions.add_argument("--show", type=int, metavar="RUN", help="detail for one run")
    actions.add_argument("--approve", type=int, metavar="RUN", help="approve a suspended run")
    actions.add_argument("--reject", type=int, metavar="RUN", help="reject a suspended run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.verbose:
        _quiet_logs()

    db = SessionLocal()
    try:
        if args.list:
            return cmd_list(db)
        if args.pending:
            return cmd_pending(db)
        if args.show is not None:
            return cmd_show(db, args.show)
        if args.approve is not None:
            return cmd_resume(db, run_id=args.approve, approved=True)
        if args.reject is not None:
            return cmd_resume(db, run_id=args.reject, approved=False)
        if args.request:
            return cmd_submit(db, patient_id=args.patient, text=args.request)

        build_parser().print_help()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
