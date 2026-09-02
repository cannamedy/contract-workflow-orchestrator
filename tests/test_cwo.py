from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.git_audit import GitClassification, audit_git
from contract_workflow.models import Stage, Verdict, WorkflowState
from contract_workflow.orchestrator import Orchestrator, OrchestratorError
from contract_workflow.outcome import make_outcome, validate_outcome
from contract_workflow.prompt_builder import PromptBuilder
from contract_workflow.runners import CodexCliRunner, MockRunner
from contract_workflow.state_machine import transition_after_outcome
from contract_workflow.state_store import StateStore, StateStoreError


class CwoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        self.state = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state)

    def tearDown(self) -> None:
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def workflow(self, mode: str = "gated", outcomes: str = ""):
        (self.project / ".contract-workflow").mkdir(exist_ok=True)
        text = f'''version: "1"
project:
  name: test-project
  path: {self.project}
mode: {mode}
authoritative_sources: []
skills: {{}}
runner:
  type: mock
  mock_outcomes:
{outcomes or '    TASK_EXECUTION: {verdict: APPROVED}\n    TASK_INDEPENDENT_REVIEW: {verdict: APPROVED}\n    FINAL_VERIFICATION: {verdict: COMPLETED}'}
policy:
  auto_patch: true
  auto_rereview: true
  auto_plan_defect_resolution: true
  auto_plan_revision_review: true
  auto_commit_checkpoint: false
  auto_push: false
  auto_tag: false
  max_attempts_per_stage: 2
  max_total_steps: 50
  retry_backoff_seconds: 0
groups:
  - id: g
    tasks:
      - id: t
'''
        path = self.project / ".contract-workflow" / "workflow.yaml"
        path.write_text(text, encoding="utf-8")
        return load_workflow(path, self.project)

    def test_state_machine_verdicts_and_autonomous(self):
        config = self.workflow("gated")
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_INDEPENDENT_REVIEW.value, current_group="g", current_task="t")
        self.assertEqual(transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", "APPROVED")).state.current_stage, Stage.HUMAN_GROUP_APPROVAL.value)
        self.assertEqual(transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", "REQUIRES_PATCH")).state.current_stage, Stage.TASK_PATCH.value)
        self.assertEqual(transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", "PLAN_TASK_DEFECT")).state.current_stage, Stage.PLAN_DEFECT_RESOLUTION.value)
        self.assertEqual(transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", "OPEN_CONTRACT_ISSUE")).state.current_stage, Stage.HARD_STOP.value)
        autonomous = self.workflow("autonomous")
        result = transition_after_outcome(autonomous, state, make_outcome("r", state.current_stage, "p", "APPROVED"))
        self.assertEqual(result.state.current_stage, Stage.FINAL_VERIFICATION.value)

    def test_outcome_validation_rejects_missing_malformed_unknown_and_mismatch(self):
        path = self.root / "outcome.json"
        self.assertFalse(validate_outcome(path, "r", "S")[0])
        path.write_text("{", encoding="utf-8")
        self.assertFalse(validate_outcome(path, "r", "S")[0])
        path.write_text(json.dumps({"verdict": "NOPE"}), encoding="utf-8")
        self.assertFalse(validate_outcome(path, "r", "S")[0])
        value = make_outcome("other", "S", "p", "APPROVED")
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(validate_outcome(path, "r", "S")[0])
        value = make_outcome("r", "OTHER", "p", "APPROVED")
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertFalse(validate_outcome(path, "r", "S")[0])

    def test_atomic_state_store_and_corrupt_state(self):
        store = StateStore(self.state)
        state = WorkflowState(project="p")
        store.save(state)
        self.assertEqual(store.load().project, "p")
        store.state_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(StateStoreError):
            store.load()

    def test_git_audit_classifies_expected_frozen_unrelated_and_conflict(self):
        config = self.workflow()
        expected = self.project / "expected.txt"
        config = config.__class__(**{**config.__dict__, "groups": (config.groups[0].__class__("g", (config.groups[0].tasks[0].__class__("t", ("expected.txt",), ()),)),)})
        expected.write_text("target", encoding="utf-8")
        (self.project / "unrelated.bin").write_text("x", encoding="utf-8")
        audit = audit_git(self.project, config)
        classes = {item.classification for item in audit.changes}
        self.assertIn(GitClassification.EXPECTED_TARGET_ARTIFACT, classes)
        self.assertIn(GitClassification.UNEXPECTED_UNRELATED_CHANGE, classes)

    def test_git_audit_classifies_merge_conflict(self):
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "CWO"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "cwo@example.invalid"], check=True)
        path = self.project / "conflict.txt"
        path.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "conflict.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.project), "checkout", "-qb", "side"], check=True)
        path.write_text("side\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "conflict.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "side"], check=True)
        subprocess.run(["git", "-C", str(self.project), "checkout", "-q", "master"], check=True)
        path.write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "conflict.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "main"], check=True)
        merge = subprocess.run(["git", "-C", str(self.project), "merge", "side"], capture_output=True, check=False)
        self.assertNotEqual(merge.returncode, 0)
        classes = {item.classification for item in audit_git(self.project, self.workflow()).changes}
        self.assertIn(GitClassification.MERGE_CONFLICT, classes)

    def test_prompt_builder_includes_bounded_context_and_contract(self):
        config = self.workflow()
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t")
        prompt = PromptBuilder().build(config, state, self.root / "run" / "outcome.json")
        self.assertIn("ORCHESTRATOR OUTPUT CONTRACT", prompt)
        self.assertIn("CURRENT STAGE: TASK_EXECUTION", prompt)
        self.assertIn("outcome.json", prompt)
        self.assertIn("Use APPROVED when the requested implementation and tests are complete", prompt)

    def test_prompt_builder_expands_task_scope_and_keeps_review_independent(self):
        config = self.workflow()
        task = config.groups[0].tasks[0].__class__("t", ("calculator.py",), ("tests/**",))
        config = config.__class__(**{**config.__dict__, "groups": (config.groups[0].__class__("g", (task,)),)})
        execution = WorkflowState(project="test-project", current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t", last_outcome={"summary": "untrusted execution prose"})
        execution_prompt = PromptBuilder().build(config, execution, self.root / "run" / "outcome.json")
        self.assertIn("- calculator.py", execution_prompt)
        self.assertIn("- tests/**", execution_prompt)
        review = WorkflowState(project="test-project", current_stage=Stage.TASK_INDEPENDENT_REVIEW.value, current_group="g", current_task="t", last_outcome={"summary": "untrusted execution prose"})
        review_prompt = PromptBuilder().build(config, review, self.root / "review" / "outcome.json")
        self.assertIn("not provided to preserve review independence", review_prompt)
        self.assertNotIn("untrusted execution prose", review_prompt)

    def test_codex_cli_runner_passes_text_prompt_over_stdin(self):
        run_dir = self.root / "codex-run"
        run_dir.mkdir()
        runner = CodexCliRunner(command=f'{os.sys.executable} -c "import sys; sys.stdin.read(); print(\'ok\')"')
        result = runner.run(self.project, "prompt text", run_dir, timeout=5)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual((run_dir / "stdout.log").read_text(encoding="utf-8").strip(), "ok")

    def test_plan_task_defect_e2e_stops_at_plan_freeze(self):
        outcomes = "    TASK_EXECUTION: {verdict: APPROVED}\n    TASK_INDEPENDENT_REVIEW: {verdict: PLAN_TASK_DEFECT}\n    PLAN_DEFECT_RESOLUTION: {verdict: APPROVED}\n    PLAN_REVISION_REVIEW: {verdict: APPROVED}\n    FINAL_VERIFICATION: {verdict: COMPLETED}\n"
        config = self.workflow("gated", outcomes)
        runner = MockRunner(config.runner.mock_outcomes)
        orchestrator = Orchestrator(config, runner=runner)
        state = orchestrator.run()
        self.assertEqual(state.current_stage, Stage.HUMAN_PLAN_FREEZE.value)
        self.assertEqual(runner.calls, [Stage.TASK_EXECUTION.value, Stage.TASK_INDEPENDENT_REVIEW.value, Stage.PLAN_DEFECT_RESOLUTION.value, Stage.PLAN_REVISION_REVIEW.value])
        self.assertEqual(list((self.project / ".git" / "refs" / "tags").iterdir()), [])
        self.assertFalse((self.project / ".git" / "index.lock").exists())
        self.assertFalse((self.project / ".contract-workflow" / "frozen").exists())

    def test_human_gate_approve_and_blocking_gate_cannot_be_approved(self):
        config = self.workflow()
        orchestrator = Orchestrator(config)
        state = orchestrator.run()
        self.assertEqual(state.current_stage, Stage.HUMAN_GROUP_APPROVAL.value)
        state = orchestrator.approve()
        self.assertEqual(state.current_stage, Stage.FINAL_VERIFICATION.value)
        state = orchestrator.run()
        self.assertEqual(state.current_stage, Stage.HUMAN_FINAL_ACCEPTANCE.value)
        self.assertEqual(orchestrator.approve().current_stage, Stage.COMPLETED.value)

    def test_mock_retry_invalid_then_approved(self):
        outcomes = "    TASK_EXECUTION:\n      - verdict: BAD\n      - verdict: APPROVED\n    TASK_INDEPENDENT_REVIEW: {verdict: APPROVED}\n    FINAL_VERIFICATION: {verdict: COMPLETED}\n"
        config = self.workflow("gated", outcomes)
        runner = MockRunner(config.runner.mock_outcomes)
        state = Orchestrator(config, runner=runner).run()
        self.assertEqual(state.current_stage, Stage.HUMAN_GROUP_APPROVAL.value)
        self.assertEqual(runner.calls.count(Stage.TASK_EXECUTION.value), 2)

    def test_runner_failure_is_bounded(self):
        outcomes = "    TASK_EXECUTION: {runner_failure: transport}\n    TASK_INDEPENDENT_REVIEW: {verdict: APPROVED}\n    FINAL_VERIFICATION: {verdict: COMPLETED}\n"
        config = self.workflow("gated", outcomes)
        runner = MockRunner(config.runner.mock_outcomes)
        state = Orchestrator(config, runner=runner).run()
        self.assertEqual(state.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(runner.calls.count(Stage.TASK_EXECUTION.value), 2)

    def test_recoverable_unrelated_change_preserves_blocked_stage_and_recovers(self):
        config = self.workflow()
        store = StateStore(self.state)
        store.save(WorkflowState(project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file, workflow_digest=config.digest, current_stage=Stage.FINAL_VERIFICATION.value, current_group="g", current_task="t"))
        (self.project / "unexpected.tmp").write_text("fixture contamination", encoding="utf-8")
        orchestrator = Orchestrator(config, store=store, runner=MockRunner())
        stopped = orchestrator.step().state
        self.assertEqual(stopped.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(stopped.stop_code, "UNEXPECTED_UNRELATED_CHANGE")
        self.assertEqual(stopped.blocked_stage, Stage.FINAL_VERIFICATION.value)
        self.assertTrue(stopped.recoverable)
        self.assertEqual(orchestrator.recover().current_stage, Stage.HARD_STOP.value)
        (self.project / "unexpected.tmp").unlink()
        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()
        self.assertEqual(recovered.current_stage, Stage.FINAL_VERIFICATION.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertIsNone(recovered.stop_code)
        self.assertFalse(recovered.recoverable)
        resumed = Orchestrator(config, store=store, runner=MockRunner()).step().state
        self.assertEqual(resumed.current_stage, Stage.HUMAN_FINAL_ACCEPTANCE.value)

    def test_recovery_rejects_nonrecoverable_stops(self):
        config = self.workflow()
        store = StateStore(self.state)
        for code in ("OPEN_CONTRACT_ISSUE", "ARCHITECTURE_DECISION_REQUIRED", "RECOVERY_UNCERTAIN"):
            with self.subTest(code=code):
                store.save(WorkflowState(project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file, workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value, blocked_stage=Stage.FINAL_VERIFICATION.value, stop_code=code, stop_reason=code, recoverable=False, status="HARD_STOPPED"))
                with self.assertRaises(OrchestratorError):
                    Orchestrator(config, store=store).recover()

    def test_crash_recovery_reconciles_completed_outcome_without_runner(self):
        config = self.workflow()
        store = StateStore(self.state)
        run_id = "completed-before-transition"
        run_dir = store.run_dir(run_id)
        state = WorkflowState(project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file, workflow_digest=config.digest, current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t", run_id=run_id, attempt=1)
        store.save(state)
        run_dir.joinpath("outcome.json").write_text(json.dumps(make_outcome(run_id, Stage.TASK_EXECUTION.value, config.project_name, "APPROVED", group="g", task="t")), encoding="utf-8")
        runner = MockRunner()
        result = Orchestrator(config, store=store, runner=runner).step()
        self.assertEqual(result.state.current_stage, Stage.TASK_INDEPENDENT_REVIEW.value)
        self.assertEqual(runner.calls, [])

    def test_frozen_source_integrity_blocks(self):
        source = self.project / "contract.md"
        source.write_text("original", encoding="utf-8")
        config = self.workflow()
        # Build this case through YAML so the public loader path is exercised.
        digest = hashlib.sha256(b"other").hexdigest()
        path = self.project / ".contract-workflow" / "workflow.yaml"
        path.write_text(path.read_text().replace("authoritative_sources: []", f"authoritative_sources:\n  - path: contract.md\n    sha256: {digest}"), encoding="utf-8")
        config = load_workflow(path, self.project)
        state = Orchestrator(config).run()
        self.assertEqual(state.current_stage, Stage.HARD_STOP.value)


if __name__ == "__main__":
    unittest.main()
