from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import default_workflow
from .orchestrator import Orchestrator, OrchestratorError, doctor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cwo", description="Contract Workflow Orchestrator v0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a project workflow contract")
    init.add_argument("project", type=Path)
    for name in ("doctor", "status", "step", "run", "resume", "recover", "approve", "stop"):
        command = sub.add_parser(name)
        command.add_argument("project", type=Path)
        if name == "run":
            command.add_argument("--dry-run", action="store_true")
        if name == "approve":
            command.add_argument("--gate", choices=["HUMAN_GROUP_APPROVAL", "HUMAN_PLAN_FREEZE", "HUMAN_FINAL_ACCEPTANCE"])
        if name == "stop":
            command.add_argument("--reason", default="stopped by operator")
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
    try:
        orchestrator = Orchestrator.for_project(args.project)
        if args.command == "status":
            _dump(orchestrator.status())
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
        return 0
    except (OrchestratorError, ValueError, OSError) as exc:
        print(f"cwo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
