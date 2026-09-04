from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import default_workflow, load_workflow
from .orchestrator import Orchestrator, OrchestratorError, doctor, state_root
from .remote import check_remote_authority
from .state_store import StateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cwo", description="Contract Workflow Orchestrator v0.7.1")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a project workflow contract")
    init.add_argument("project", type=Path)
    for name in ("doctor", "status", "step", "run", "resume", "recover", "decisions", "approve", "stop"):
        command = sub.add_parser(name)
        command.add_argument("project", type=Path)
        if name == "run":
            command.add_argument("--dry-run", action="store_true")
        if name == "approve":
            command.add_argument("--gate", choices=["HUMAN_GROUP_APPROVAL", "HUMAN_PLAN_FREEZE", "HUMAN_FINAL_ACCEPTANCE"])
        if name == "stop":
            command.add_argument("--reason", default="stopped by operator")
    decision = sub.add_parser("decision", help="inspect a persisted human decision")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)
    decision_show = decision_sub.add_parser("show")
    decision_show.add_argument("project", type=Path)
    decision_show.add_argument("decision_id")
    decide = sub.add_parser("decide", help="resolve a pending human decision")
    decide.add_argument("project", type=Path)
    decide.add_argument("decision_id")
    decide.add_argument("--option")
    decide.add_argument("--answer")
    decide.add_argument("--rationale")
    authority = sub.add_parser("authority", help="inspect submitted remote authority")
    authority_sub = authority.add_subparsers(dest="authority_command", required=True)
    check = authority_sub.add_parser("check", help="check remote authority once")
    check.add_argument("project", type=Path)
    check.add_argument("--dry-run", action="store_true")
    watch = authority_sub.add_parser("watch", help="poll remote authority")
    watch.add_argument("project", type=Path)
    watch.add_argument("--interval", type=float, default=300.0)
    watch.add_argument("--dry-run", action="store_true")
    return parser


def _dump(value: object) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        project = args.project.expanduser().resolve()
        project.mkdir(parents=True, exist_ok=True)
        workflow = project / ".contract-workflow" / "workflow.yaml"
        if workflow.exists():
            print(f"workflow already exists: {workflow}", file=sys.stderr)
            return 2
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(default_workflow(project), encoding="utf-8")
        print(workflow)
        return 0
    if args.command == "doctor":
        report = doctor(args.project)
        _dump(report)
        return 0 if report.get("ok") else 1
    if args.command == "authority":
        project = args.project.expanduser().resolve()
        workflow = project / ".contract-workflow" / "workflow.yaml"
        config = load_workflow(workflow, project_override=project)
        store = StateStore(state_root(project))
        if args.authority_command == "check":
            result = check_remote_authority(config, store, store.load(), dry_run=args.dry_run)
            _dump({"status": result.status, "changed": result.changed, "snapshot": result.snapshot.to_dict() if result.snapshot else None, "change": result.new_change, "rollover": result.rollover, "errors": list(result.errors), "dry_run": args.dry_run})
            return 0 if not result.errors else 1
        if args.interval <= 0:
            print("cwo: --interval must be positive", file=sys.stderr)
            return 2
        try:
            while True:
                result = check_remote_authority(config, store, store.load(), dry_run=args.dry_run)
                _dump({"status": result.status, "changed": result.changed, "snapshot": result.snapshot.to_dict() if result.snapshot else None, "change": result.new_change, "rollover": result.rollover, "errors": list(result.errors), "dry_run": args.dry_run})
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    try:
        orchestrator = Orchestrator.for_project(args.project)
        if args.command == "status":
            _dump(orchestrator.status_report())
        elif args.command == "resume":
            _dump(orchestrator.run())
        elif args.command == "recover":
            _dump(orchestrator.recover())
        elif args.command == "step":
            _dump(orchestrator.step().state)
        elif args.command == "run":
            _dump(orchestrator.run(dry_run=args.dry_run))
        elif args.command == "approve":
            _dump(orchestrator.approve(args.gate))
        elif args.command == "stop":
            _dump(orchestrator.stop(args.reason))
        elif args.command == "decisions":
            _dump([item.to_dict() for item in orchestrator.list_decisions()])
        elif args.command == "decision" and args.decision_command == "show":
            _dump(orchestrator.show_decision(args.decision_id))
        elif args.command == "decide":
            _dump(orchestrator.decide(args.decision_id, option=args.option, answer=args.answer, rationale=args.rationale))
        return 0
    except (OrchestratorError, ValueError, OSError) as exc:
        print(f"cwo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
