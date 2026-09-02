from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from contract_workflow.authority import scan_authority_changes, validate_analysis
from contract_workflow.config import load_workflow
from contract_workflow.models import Stage, Verdict, WorkItemStatus, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.outcome import make_outcome
from contract_workflow.runners.base import RunnerResult, run_times
from contract_workflow.scheduler import ready_work
from contract_workflow.state_store import StateStore


class AnalysisRunner:
    def __init__(self, store: StateStore, spec: dict):
        self.store = store
        self.spec = spec

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        state = self.store.load()
        change = state.authority_changes[state.current_authority_change_id]
        authority = {**self.spec, "change_id": change["change_id"], "base_sha256": change["base_sha256"], "candidate_sha256": change["candidate_sha256"]}
        extra = {}
        verdict = "APPROVED"
        if authority.get("human_decision_required"):
            verdict = Verdict.ARCHITECTURE_DECISION_REQUIRED.value
            extra = {"decision_requests": authority["human_decision_requests"], "directly_affected_work": authority["directly_affected_tasks"], "blocking_scope": {"directly_blocked_items": authority["directly_affected_tasks"]}}
        outcome = make_outcome(env["CWO_RUN_ID"], Stage.AUTHORITY_CHANGE_ANALYSIS.value, state.project, verdict, authority_change=authority, **extra)
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class MutatingRunner:
    def __init__(self, source: Path):
        self.source = source

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        self.source.write_text("mutation during invocation\n", encoding="utf-8")
        outcome = make_outcome(env["CWO_RUN_ID"], Stage.TASK_EXECUTION.value, cwd.name, "APPROVED")
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class AuthorityChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)
        self.source = self.project / "guide.md"
        self.source.write_text("accepted\n", encoding="utf-8")
        self.old_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def config(self):
        control = self.project / ".contract-workflow"
        control.mkdir(exist_ok=True)
        lines = [
            'version: "1"', 'project:', f'  name: authority-fixture', f'  path: {self.project}',
            'mode: autonomous', 'authoritative_sources:', '  - source_id: human-guide',
            '    role: HUMAN_GUIDE', '    path: guide.md', f'    sha256: {self.old_sha}',
            'skills: {}', 'runner:', '  type: mock', 'groups:', '  - id: g', '    tasks:',
            '      - id: A', '        requirement_ids: [REQ-A]', '      - id: B',
            '        dependencies: [A]', '      - id: X', '',
        ]
        (control / "workflow.yaml").write_text("\n".join(lines), encoding="utf-8")
        return load_workflow(control / "workflow.yaml", self.project)

    def detect(self):
        config = self.config()
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        self.source.write_text("candidate\n", encoding="utf-8")
        result = orchestrator.step()
        self.assertEqual(result.action, "authority_change_analysis")
        self.assertEqual(result.state.current_stage, Stage.AUTHORITY_CHANGE_ANALYSIS.value)
        self.assertTrue((store.authority_path / "ledger.json").is_file())
        self.assertEqual(len(list(store.authority_changes_path.glob("CR-*.json"))), 1)
        return config, store

    def c2_spec(self):
        return {
            "classification": "C2", "semantic_change": True,
            "affected_requirements": ["REQ-A"], "affected_contract_anchors": [],
            "directly_affected_tasks": ["A"], "dependency_affected_tasks": ["B"], "unaffected_tasks": ["X"],
            "machine_resolvable": True, "human_decision_required": False,
            "human_decision_requests": [], "required_propagation": ["CONTRACT_REVISION_REQUIRED"],
            "analysis_summary": "The accepted behavior changed.",
        }

    def test_external_change_enters_analysis_and_scoped_c2_continues_unaffected_work(self):
        config, store = self.detect()
        result = Orchestrator(config, store=store, runner=AnalysisRunner(store, self.c2_spec())).step()
        self.assertEqual(result.action, "authority_change_analyzed")
        self.assertEqual(result.state.work_items["A"].status, WorkItemStatus.BLOCKED_BY_AUTHORITY_CHANGE.value)
        self.assertEqual(result.state.work_items["B"].status, WorkItemStatus.WAITING_DEPENDENCY.value)
        self.assertEqual(result.state.current_task, "X")
        self.assertEqual(result.state.work_items["X"].status, WorkItemStatus.RUNNING.value)
        self.assertEqual(result.state.current_stage, Stage.TASK_EXECUTION.value)

        continued = Orchestrator(config, store=store, runner=__import__("contract_workflow.runners", fromlist=["MockRunner"]).MockRunner({Stage.TASK_EXECUTION.value: {"verdict": "APPROVED"}})).step()
        self.assertNotEqual(continued.action, "authority_change_analysis")

    def test_c0_auto_accepts_and_does_not_change_workflow_digest(self):
        config, store = self.detect()
        digest = config.digest
        spec = {**self.c2_spec(), "classification": "C0", "semantic_change": False, "affected_requirements": [], "directly_affected_tasks": [], "dependency_affected_tasks": [], "unaffected_tasks": ["A", "B", "X"], "required_propagation": []}
        result = Orchestrator(config, store=store, runner=AnalysisRunner(store, spec)).step()
        self.assertEqual(result.state.current_task, "A")
        self.assertEqual(json.loads(store.authority_ledger_path.read_text())["sources"]["human-guide"]["status"], "ACCEPTED")
        self.assertEqual(config.digest, digest)

    def test_active_agent_mutation_is_still_a_global_hard_stop(self):
        config = self.config()
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        running = orchestrator.step().state
        run_id = "active-agent"
        run_dir = store.run_dir(run_id)
        (run_dir / "metadata.json").write_text(json.dumps({"run_id": run_id, "stage": Stage.TASK_EXECUTION.value, "status": "running"}), encoding="utf-8")
        store.save(replace(running, run_id=run_id, current_stage=Stage.TASK_EXECUTION.value, status="RUNNING"))
        self.source.write_text("unauthorized\n", encoding="utf-8")
        stopped = Orchestrator(config, store=store).step().state
        self.assertEqual(stopped.stop_code, "UNAUTHORIZED_AUTHORITY_MUTATION")
        self.assertEqual(stopped.current_stage, Stage.HARD_STOP.value)

    def test_mutation_during_agent_invocation_is_hard_stopped(self):
        config = self.config()
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        stopped = Orchestrator(config, store=store, runner=MutatingRunner(self.source)).step().state
        self.assertEqual(stopped.stop_code, "UNAUTHORIZED_AUTHORITY_MUTATION")

    def test_analysis_rejects_unknown_task_hash_and_dependency_closure(self):
        config, store = self.detect()
        state = store.load()
        change = state.authority_changes[state.current_authority_change_id]
        bad = {**self.c2_spec(), "change_id": change["change_id"], "base_sha256": change["base_sha256"], "candidate_sha256": "0" * 64, "directly_affected_tasks": ["missing"], "dependency_affected_tasks": []}
        valid, errors = validate_analysis(config, store, state, make_outcome("r", Stage.AUTHORITY_CHANGE_ANALYSIS.value, config.project_name, Verdict.APPROVED.value, authority_change=bad))
        self.assertFalse(valid)
        self.assertTrue(any("unknown task" in error or "candidate_sha256" in error for error in errors))

    def test_c3_human_decision_reuses_existing_scoped_decision_system(self):
        config, store = self.detect()
        request = {
            "decision_id": "ADR-AUTH-001", "category": "ARCHITECTURE",
            "question": "Which compatible authority semantics apply?",
            "context": "The revised authority has two valid interpretations.",
            "why_human_required": "No unique compatibility policy is authoritative.",
            "options": ["old", "new"], "recommended_option": "new",
            "allow_freeform": False, "source_change": "CR authority change",
            "affected_requirements": ["REQ-A"], "affected_contract_anchors": [],
            "affected_tasks": ["A"], "directly_blocked_items": ["A"],
        }
        spec = {**self.c2_spec(), "classification": "C3", "machine_resolvable": False, "human_decision_required": True, "human_decision_requests": [request]}
        result = Orchestrator(config, store=store, runner=AnalysisRunner(store, spec)).step()
        self.assertEqual(result.state.current_stage, Stage.TASK_EXECUTION.value)
        # A READY independent branch is selected; the same persisted HumanDecision
        # machinery owns the affected scope.
        self.assertIn("ADR-AUTH-001", result.state.decisions)
        self.assertEqual(result.state.work_items["A"].status, WorkItemStatus.BLOCKED_BY_AUTHORITY_CHANGE.value)
        self.assertEqual(result.state.current_task, "X")

    def test_registered_change_is_idempotent_and_survives_restart(self):
        config, store = self.detect()
        first = scan_authority_changes(config, store, store.load())
        second = scan_authority_changes(config, store, store.load())
        self.assertEqual(first.changes[0]["change_id"], second.changes[0]["change_id"])
        self.assertEqual(len(list(store.authority_changes_path.glob("CR-*.json"))), 1)
        restarted = Orchestrator(config, store=StateStore(self.state_root))
        self.assertEqual(restarted.step().state.current_stage, Stage.AUTHORITY_CHANGE_ANALYSIS.value)
