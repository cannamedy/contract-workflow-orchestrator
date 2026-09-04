from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.git_audit import GitClassification, audit_git, source_integrity, working_tree_paths
from contract_workflow.models import AuthoritativeSource, DecisionStatus, Stage, Verdict, WorkItemStatus, WorkflowState
from contract_workflow.orchestrator import Orchestrator, OrchestratorError
from contract_workflow.outcome import make_outcome, validate_outcome
from contract_workflow.prompt_builder import PromptBuilder
from contract_workflow.runners import CodexCliRunner, MockRunner
from contract_workflow.runners.codex_cli import _bind_execution_directory, _ensure_workspace_execution_flags
from contract_workflow.state_machine import transition_after_outcome
from contract_workflow.state_store import StateStore, StateStoreError
from contract_workflow.workspace import RunWorkspace


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
        self.assertEqual(transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", "OPEN_CONTRACT_ISSUE")).state.current_stage, Stage.WAITING_FOR_HUMAN.value)
        autonomous = self.workflow("autonomous")
        result = transition_after_outcome(autonomous, state, make_outcome("r", state.current_stage, "p", "APPROVED"))
        self.assertEqual(result.state.current_stage, Stage.FINAL_VERIFICATION.value)

    def test_outcome_validation_rejects_missing_malformed_unknown_and_mismatch(self):
        path = self.root / "outcome.json"
        self.assertFalse(validate_outcome(path, "r", "S")[0])

    def test_scoped_decision_outcome_requires_machine_scope(self):
        path = self.root / "decision-outcome.json"
        value = make_outcome("r", Stage.TASK_EXECUTION.value, "p", Verdict.ARCHITECTURE_DECISION_REQUIRED.value)
        path.write_text(json.dumps(value), encoding="utf-8")
        valid, _, errors = validate_outcome(path, "r", Stage.TASK_EXECUTION.value)
        self.assertFalse(valid)
        self.assertIn("scoped decision outcome requires decision_id or decision_requests", errors)
        value = make_outcome(
            "r", Stage.TASK_EXECUTION.value, "p", Verdict.ARCHITECTURE_DECISION_REQUIRED.value,
            decision_id="D1", directly_affected_work=["t"], blocking_scope={"type": "architecture"},
        )
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertTrue(validate_outcome(path, "r", Stage.TASK_EXECUTION.value)[0])
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

    def test_workflow_digest_stop_remains_terminal_when_authority_candidate_exists(self):
        config = self.workflow("autonomous")
        original = Orchestrator(config, store=StateStore(self.state), runner=MockRunner())
        original._load_or_initialize()

        workflow_path = self.project / ".contract-workflow" / "workflow.yaml"
        workflow_path.write_text(workflow_path.read_text(encoding="utf-8") + "\n# changed after the run baseline\n", encoding="utf-8")
        changed_config = load_workflow(workflow_path, self.project)
        restarted = Orchestrator(changed_config, store=StateStore(self.state), runner=MockRunner())

        stopped = restarted.run()

        self.assertEqual(stopped.status, "HARD_STOPPED")
        self.assertEqual(stopped.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(stopped.stop_code, "WORKFLOW_DIGEST_CHANGED")
        self.assertEqual(stopped.total_steps, 0)

    def test_explicit_recovery_accepts_workflow_digest_change_between_runs(self):
        config = self.workflow("autonomous")
        original = Orchestrator(config, store=StateStore(self.state), runner=MockRunner())
        original._load_or_initialize()

        workflow_path = self.project / ".contract-workflow" / "workflow.yaml"
        workflow_path.write_text(workflow_path.read_text(encoding="utf-8") + "\n# changed between bounded runs\n", encoding="utf-8")
        changed_config = load_workflow(workflow_path, self.project)
        restarted = Orchestrator(changed_config, store=StateStore(self.state), runner=MockRunner())
        self.assertEqual(restarted.run().stop_code, "WORKFLOW_DIGEST_CHANGED")
        (self.project / "preexisting-user-change.txt").write_text("preserve\n", encoding="utf-8")

        recovered = restarted.recover()

        self.assertEqual(recovered.status, "RUNNING")
        self.assertEqual(recovered.workflow_digest, changed_config.digest)
        self.assertEqual(recovered.current_stage, Stage.INITIALIZING.value)

    def test_runner_failure_recovery_retries_without_an_outcome_artifact(self):
        config = self.workflow("autonomous")
        store = StateStore(self.state)
        run_id = "runner-failed"
        run_dir = store.run_dir(run_id)
        (run_dir / "metadata.json").write_text(json.dumps({
            "run_id": run_id,
            "stage": Stage.AUTHORITY_CHANGE_ANALYSIS.value,
            "status": "completed",
            "finished_at": "2026-09-04T00:00:00+00:00",
            "exit_code": 1,
            "timed_out": False,
        }), encoding="utf-8")
        store.save(WorkflowState(
            project=config.project_name,
            project_path=config.project_path,
            workflow_file=config.workflow_file,
            workflow_digest=config.digest,
            current_stage=Stage.HARD_STOP.value,
            blocked_stage=Stage.AUTHORITY_CHANGE_ANALYSIS.value,
            stop_code="RETRY_EXHAUSTED",
            stop_reason="runner process failed with exit code 1",
            recoverable=True,
            status="HARD_STOPPED",
            last_outcome={"verdict": Verdict.INVALID_OUTCOME.value, "summary": "runner process failed with exit code 1"},
        ))

        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()

        self.assertEqual(recovered.status, "RUNNING")
        self.assertEqual(recovered.current_stage, Stage.AUTHORITY_CHANGE_ANALYSIS.value)

    def test_interrupted_agent_recovery_requires_no_live_process_and_unchanged_target(self):
        config = self.workflow("autonomous")
        store = StateStore(self.state)
        run_id = "interrupted-run"
        workspace = RunWorkspace.create(self.project, self.state, run_id)
        run_dir = store.run_dir(run_id)
        metadata = {
            "run_id": run_id,
            "stage": Stage.AUTHORITY_CHANGE_ANALYSIS.value,
            "status": "running",
            "workspace_path": str(workspace),
            "workspace_baseline": workspace.baseline,
            "real_baseline": workspace.real_baseline,
            "excluded_roots": [str(item) for item in workspace.excluded_roots],
        }
        (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        store.save(WorkflowState(
            project=config.project_name,
            project_path=config.project_path,
            workflow_file=config.workflow_file,
            workflow_digest=config.digest,
            current_stage=Stage.HARD_STOP.value,
            blocked_stage=Stage.AUTHORITY_CHANGE_ANALYSIS.value,
            run_id=run_id,
            stop_code="RECOVERY_UNCERTAIN",
            stop_reason="prior Agent invocation has no completed artifact",
            status="HARD_STOPPED",
        ))

        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()

        self.assertEqual(recovered.status, "RUNNING")
        self.assertEqual(recovered.current_stage, Stage.AUTHORITY_CHANGE_ANALYSIS.value)
        self.assertEqual(json.loads((run_dir / "metadata.json").read_text())["status"], "failed")

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

    def test_frozen_source_allows_descendant_commit_without_source_change(self):
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "CWO"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "cwo@example.invalid"], check=True)
        source = self.project / "spec.md"
        source.write_text("accepted\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "spec.md"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "freeze spec"], check=True)
        frozen_commit = subprocess.check_output(["git", "-C", str(self.project), "rev-parse", "HEAD"], text=True).strip()
        (self.project / "unrelated.txt").write_text("infrastructure\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "unrelated.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "unrelated infrastructure"], check=True)
        frozen = AuthoritativeSource("spec.md", hashlib.sha256(source.read_bytes()).hexdigest(), git_commit=frozen_commit)

        self.assertEqual(source_integrity(self.project, (frozen,)), [])

        source.write_text("changed\n", encoding="utf-8")
        self.assertTrue(source_integrity(self.project, (frozen,)))

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

    def test_preexisting_unrelated_change_is_preserved_at_invocation_baseline(self):
        config = self.workflow()
        (self.project / "user-notes.txt").write_text("draft\n", encoding="utf-8")
        baseline = working_tree_paths(self.project)
        audit = audit_git(self.project, config, baseline_paths=baseline)
        self.assertIn(GitClassification.PRESERVED_BASELINE_CHANGE, {item.classification for item in audit.changes})
        self.assertFalse(audit.blocking)

    def test_orchestrator_dry_start_does_not_block_preexisting_unrelated_change(self):
        config = self.workflow()
        (self.project / "user-notes.txt").write_text("draft\n", encoding="utf-8")
        state = Orchestrator(config, runner=MockRunner()).step().state
        self.assertEqual(state.current_stage, Stage.TASK_EXECUTION.value)
        self.assertEqual(state.status, "RUNNING")

    def test_prompt_builder_includes_bounded_context_and_contract(self):
        config = self.workflow()
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t")
        prompt = PromptBuilder().build(config, state, self.root / "run" / "outcome.json")
        self.assertIn("ORCHESTRATOR OUTPUT CONTRACT", prompt)
        self.assertIn("CURRENT STAGE: TASK_EXECUTION", prompt)
        self.assertIn("outcome.json", prompt)
        self.assertIn("Use APPROVED when the requested implementation and tests are complete", prompt)

    def test_prompt_distinguishes_execution_workspace_from_authoritative_origin(self):
        config = self.workflow()
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t")
        workspace = self.root / "state" / "workspaces" / "run" / "project"
        prompt = PromptBuilder().build(config, state, self.root / "run" / "outcome.json", execution_workspace=workspace)
        self.assertIn(f"EXECUTION WORKSPACE: {workspace}", prompt)
        self.assertIn(f"AUTHORITATIVE ORIGIN: {self.project}", prompt)
        self.assertIn("Do not access or modify the authoritative origin repository directly", prompt)

    def test_review_prompt_contains_complete_nested_issue_schema(self):
        config = self.workflow()
        run_id = "exact-review-run"
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_INDEPENDENT_REVIEW.value, current_group="g", current_task="t", run_id=run_id)
        prompt = PromptBuilder().build(config, state, self.root / "run" / "outcome.json")
        for field in ("type", "severity", "requirement_ids", "message", "blocking", "recommended_stage"):
            self.assertIn(f'"{field}"', prompt)
        self.assertIn(f'run_id=\"{run_id}\"', prompt)
        self.assertIn('"stage": "TASK_INDEPENDENT_REVIEW"', prompt)
        self.assertIn("issues: []", prompt)

    def test_validate_outcome_accepts_complete_issue(self):
        path = self.root / "outcome.json"
        issue = {
            "type": "IMPLEMENTATION_DEFECT",
            "severity": "medium",
            "requirement_ids": ["REQ-001"],
            "message": "specific issue",
            "blocking": False,
            "recommended_stage": "TASK_PATCH",
        }
        value = make_outcome("r", Stage.TASK_INDEPENDENT_REVIEW.value, "p", "REQUIRES_PATCH", issues=[issue])
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(validate_outcome(path, "r", Stage.TASK_INDEPENDENT_REVIEW.value)[0], True)

    def test_validate_outcome_rejects_issue_missing_each_required_field(self):
        required = ("type", "severity", "requirement_ids", "message", "blocking", "recommended_stage")
        for missing in required:
            with self.subTest(missing=missing):
                path = self.root / f"outcome-{missing}.json"
                issue = {
                    "type": "IMPLEMENTATION_DEFECT",
                    "severity": "medium",
                    "requirement_ids": ["REQ-001"],
                    "message": "specific issue",
                    "blocking": False,
                    "recommended_stage": "TASK_PATCH",
                }
                issue.pop(missing)
                value = make_outcome("r", Stage.TASK_INDEPENDENT_REVIEW.value, "p", "REQUIRES_PATCH", issues=[issue])
                path.write_text(json.dumps(value), encoding="utf-8")
                valid, _, errors = validate_outcome(path, "r", Stage.TASK_INDEPENDENT_REVIEW.value)
                self.assertFalse(valid)
                self.assertIn(f"issues[0] missing field: {missing}", errors)

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
        self.assertEqual(result.runner_metadata["effective_cwd"], str(self.project.resolve()))
        self.assertEqual(result.runner_metadata["sandbox_mode"], "default")

    def test_codex_runner_binds_danger_full_access_to_workspace_and_records_argv(self):
        run_dir = self.root / "codex-bound-run"
        run_dir.mkdir()
        workspace = self.root / "workspace"
        workspace.mkdir()
        runner = CodexCliRunner(command=f'{os.sys.executable} -c "import os; print(os.getcwd())" --sandbox danger-full-access -C {self.project}')
        result = runner.run(workspace, "ignored", run_dir, timeout=5)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.runner_metadata["effective_cwd"], str(workspace.resolve()))
        self.assertEqual(result.runner_metadata["sandbox_mode"], "danger-full-access")
        self.assertIn(str(workspace.resolve()), result.runner_metadata["argv"])
        self.assertNotIn(str(self.project.resolve()), result.runner_metadata["argv"])

    def test_codex_command_cannot_keep_configured_authoritative_cwd(self):
        command = ["codex", "exec", "-C", str(self.project), "-"]
        workspace = self.root / "state" / "workspaces" / "run" / "project"
        self.assertEqual(_bind_execution_directory(command, workspace)[3], str(workspace))

    def test_codex_workspace_flags_are_inserted_before_stdin_prompt(self):
        command = _ensure_workspace_execution_flags(["codex", "exec", "-C", "/shadow", "-"])
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "-")
        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)
        self.assertLess(command.index("--skip-git-repo-check"), command.index("-"))

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
        continued = orchestrator.step().state
        self.assertEqual(continued.current_stage, Stage.HUMAN_FINAL_ACCEPTANCE.value)
        self.assertEqual(continued.status, "WAITING_HUMAN")
        self.assertIsNone(continued.stop_code)
        self.assertFalse(continued.recoverable)

    def test_schema_recovery_restores_exact_read_only_stage_without_coding(self):
        config = self.workflow()
        store = StateStore(self.state)
        run_id = "invalid-review-run"
        run_dir = store.run_dir(run_id)
        invalid = make_outcome(
            run_id, Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "REQUIRES_PATCH",
            group="g", task="t", issues=[{"severity": "medium", "requirement_ids": [], "blocking": True}],
        )
        run_dir.joinpath("outcome.json").write_text(json.dumps(invalid), encoding="utf-8")
        run_dir.joinpath("metadata.json").write_text(json.dumps({
            "run_id": run_id, "stage": Stage.TASK_INDEPENDENT_REVIEW.value, "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
            "exit_code": 0, "timed_out": False,
        }), encoding="utf-8")
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
            workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value,
            current_group="g", current_task="t", run_id=run_id, attempt=3,
            last_successful_stage=Stage.TASK_EXECUTION.value,
            last_outcome=make_outcome("prior", Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "INVALID_OUTCOME"),
            stop_code="RETRY_EXHAUSTED", stop_reason="issues[0] missing field: type", status="HARD_STOPPED",
        )
        store.save(state)
        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()
        self.assertEqual(recovered.current_stage, Stage.TASK_INDEPENDENT_REVIEW.value)
        self.assertEqual(recovered.attempt, 0)
        self.assertIsNone(recovered.run_id)
        self.assertEqual(recovered.status, "RUNNING")

    def test_schema_recovery_rejects_running_or_non_schema_latest_run(self):
        config = self.workflow()
        for status, exit_code, timed_out in (("running", 0, False), ("completed", 1, False)):
            with self.subTest(status=status, exit_code=exit_code):
                store = StateStore(self.state / f"{status}-{exit_code}")
                run_id = "unsafe-run"
                run_dir = store.run_dir(run_id)
                run_dir.joinpath("outcome.json").write_text(json.dumps(make_outcome(run_id, Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "REQUIRES_PATCH", issues=[])), encoding="utf-8")
                run_dir.joinpath("metadata.json").write_text(json.dumps({
                    "run_id": run_id, "stage": Stage.TASK_INDEPENDENT_REVIEW.value, "status": status,
                    "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
                    "exit_code": exit_code, "timed_out": timed_out,
                }), encoding="utf-8")
                store.save(WorkflowState(
                    project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
                    workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value,
                    blocked_stage=Stage.TASK_INDEPENDENT_REVIEW.value, run_id=run_id, attempt=3,
                    last_successful_stage=Stage.TASK_EXECUTION.value,
                    last_outcome=make_outcome("prior", Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "INVALID_OUTCOME"),
                    stop_code="RETRY_EXHAUSTED", stop_reason="schema validation failed", status="HARD_STOPPED",
                ))
                with self.assertRaises(OrchestratorError):
                    Orchestrator(config, store=store).recover()

    def _late_recovery_fixture(self, config, stage, outcome, *, run_id="late-run", store_name="late"):
        store = StateStore(self.state / store_name)
        run_dir = store.run_dir(run_id)
        run_dir.joinpath("outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        run_dir.joinpath("metadata.json").write_text(json.dumps({
            "run_id": run_id, "stage": stage, "status": "completed",
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00",
            "exit_code": 0, "timed_out": False,
        }), encoding="utf-8")
        store.save(WorkflowState(
            project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
            workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value,
            current_group="g", current_task="t", run_id=run_id, attempt=3,
            last_successful_stage=Stage.TASK_EXECUTION.value,
            last_outcome=make_outcome("prior", stage, config.project_name, "INVALID_OUTCOME"),
            stop_code="RETRY_EXHAUSTED", stop_reason="outcome.json is missing", status="HARD_STOPPED",
            blocked_stage=stage,
        ))
        return store

    def test_late_valid_review_outcome_reconciles_through_canonical_transition(self):
        config = self.workflow()
        run_id = "late-review-patch"
        issue = {
            "type": "IMPLEMENTATION_DEFECT", "severity": "high", "requirement_ids": ["REQ-001"],
            "message": "patch this defect", "blocking": True, "recommended_stage": "TASK_PATCH",
        }
        outcome = make_outcome(run_id, Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "REQUIRES_PATCH", issues=[issue])
        store = self._late_recovery_fixture(config, Stage.TASK_INDEPENDENT_REVIEW.value, outcome, run_id=run_id)
        runner = MockRunner()
        recovered = Orchestrator(config, store=store, runner=runner).recover()
        self.assertEqual(recovered.current_stage, Stage.TASK_PATCH.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertIsNone(recovered.run_id)
        self.assertEqual(recovered.last_outcome["verdict"], Verdict.REQUIRES_PATCH.value)
        self.assertEqual(runner.calls, [])
        events = [json.loads(line)["event"] for line in store.events_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("late_outcome_detected", events)
        self.assertIn("late_outcome_validated", events)
        self.assertIn("late_outcome_reconciled", events)

    def test_late_valid_approved_review_outcome_uses_normal_next_stage(self):
        config = self.workflow()
        run_id = "late-review-approved"
        outcome = make_outcome(run_id, Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "APPROVED")
        store = self._late_recovery_fixture(config, Stage.TASK_INDEPENDENT_REVIEW.value, outcome, run_id=run_id, store_name="late-approved")
        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()
        self.assertEqual(recovered.current_stage, Stage.HUMAN_GROUP_APPROVAL.value)
        self.assertEqual(recovered.status, "WAITING_HUMAN")
        self.assertEqual(recovered.last_outcome["verdict"], Verdict.APPROVED.value)

    def test_late_outcome_rejects_run_id_and_stage_mismatch(self):
        config = self.workflow()
        cases = (
            ("late-run-id-mismatch", make_outcome("other-run", Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name, "APPROVED")),
            ("late-stage-mismatch", make_outcome("late-stage-mismatch", Stage.TASK_PATCH.value, config.project_name, "APPROVED")),
        )
        for store_name, outcome in cases:
            with self.subTest(store_name=store_name):
                run_id = "late-run-id-mismatch" if store_name == "late-run-id-mismatch" else store_name
                store = self._late_recovery_fixture(config, Stage.TASK_INDEPENDENT_REVIEW.value, outcome, run_id=run_id, store_name=store_name)
                with self.assertRaises(OrchestratorError):
                    Orchestrator(config, store=store).recover()
                self.assertEqual(store.load().current_stage, Stage.HARD_STOP.value)

    def test_late_valid_mutating_stage_outcome_is_rejected(self):
        config = self.workflow()
        run_id = "late-execution"
        outcome = make_outcome(run_id, Stage.TASK_EXECUTION.value, config.project_name, "APPROVED")
        store = self._late_recovery_fixture(config, Stage.TASK_EXECUTION.value, outcome, run_id=run_id, store_name="late-execution")
        with self.assertRaises(OrchestratorError):
            Orchestrator(config, store=store).recover()
        self.assertEqual(store.load().current_stage, Stage.HARD_STOP.value)

    def test_late_scoped_decision_outcome_is_persisted_during_recovery(self):
        config = self.workflow()
        run_id = "late-architecture-review"
        outcome = make_outcome(
            run_id, Stage.TASK_INDEPENDENT_REVIEW.value, config.project_name,
            Verdict.ARCHITECTURE_DECISION_REQUIRED.value,
            decision_id="ADR-LATE-001", directly_affected_work=["t"],
            question="Which architecture boundary applies?", options=["defer", "adopt"],
        )
        store = self._late_recovery_fixture(config, Stage.TASK_INDEPENDENT_REVIEW.value, outcome, run_id=run_id, store_name="late-architecture")
        recovered = Orchestrator(config, store=store, runner=MockRunner()).recover()
        self.assertEqual(recovered.current_stage, Stage.WAITING_FOR_HUMAN.value)
        self.assertEqual(recovered.decisions["ADR-LATE-001"].status, DecisionStatus.PENDING.value)
        self.assertTrue((store.decisions_path / "ADR-LATE-001.json").is_file())

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

    def test_external_authority_change_enters_analysis_instead_of_global_stop(self):
        source = self.project / "contract.md"
        source.write_text("original", encoding="utf-8")
        config = self.workflow()
        # Build this case through YAML so the public loader path is exercised.
        digest = hashlib.sha256(b"other").hexdigest()
        path = self.project / ".contract-workflow" / "workflow.yaml"
        path.write_text(path.read_text().replace("authoritative_sources: []", f"authoritative_sources:\n  - path: contract.md\n    sha256: {digest}"), encoding="utf-8")
        config = load_workflow(path, self.project)
        state = Orchestrator(config).step().state
        self.assertEqual(state.current_stage, Stage.AUTHORITY_CHANGE_ANALYSIS.value)
        self.assertEqual(state.status, "RUNNING")

    def scoped_fixture(self):
        source = Path(__file__).parent / "fixtures" / "scoped-human-gate" / ".contract-workflow"
        destination = self.project / ".contract-workflow"
        shutil.copytree(source, destination)
        return load_workflow(destination / "workflow.yaml", self.project)

    def test_scoped_decisions_persist_and_unaffected_work_completes(self):
        config = self.scoped_fixture()
        runner = MockRunner(config.runner.mock_outcomes)
        orchestrator = Orchestrator(config, runner=runner)
        state = orchestrator.run()
        self.assertEqual(state.current_stage, Stage.WAITING_FOR_HUMAN.value)
        self.assertEqual(state.status, "WAITING_HUMAN")
        self.assertEqual(set(state.decisions), {"ADR-PENDING-001", "ADR-PENDING-002"})
        self.assertEqual(state.work_items["TASK-002"].status, WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value)
        self.assertEqual(state.work_items["TASK-006"].status, WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value)
        self.assertEqual(state.work_items["TASK-009"].status, WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value)
        self.assertEqual(state.work_items["TASK-012"].status, WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value)
        self.assertEqual(state.work_items["TASK-Y"].status, WorkItemStatus.WAITING_DEPENDENCY.value)
        self.assertEqual(state.work_items["TASK-X"].status, WorkItemStatus.COMPLETED.value)
        self.assertEqual(runner.task_calls.count("TASK-X"), 2)
        self.assertTrue((self.state / "decisions" / "ADR-PENDING-001.json").is_file())
        reloaded = Orchestrator(config, store=StateStore(self.state), runner=MockRunner()).list_decisions()
        self.assertEqual({item.decision_id for item in reloaded}, {"ADR-PENDING-001", "ADR-PENDING-002"})

    def test_resolving_one_decision_preserves_other_blocker_and_resumes_ready_work(self):
        config = self.scoped_fixture()
        runner = MockRunner(config.runner.mock_outcomes)
        orchestrator = Orchestrator(config, runner=runner)
        orchestrator.run()
        after_d1 = orchestrator.decide("ADR-PENDING-001", option="defer", rationale="keep the revision outside this slice")
        self.assertEqual(after_d1.current_stage, Stage.WAITING_FOR_HUMAN.value)
        self.assertEqual(after_d1.work_items["TASK-002"].blocking_decision_ids, ["ADR-PENDING-002"])
        self.assertEqual(after_d1.work_items["TASK-006"].blocking_decision_ids, ["ADR-PENDING-002"])
        self.assertEqual(after_d1.work_items["TASK-009"].status, WorkItemStatus.WAITING_DEPENDENCY.value)
        self.assertEqual(after_d1.decisions["ADR-PENDING-001"].status, DecisionStatus.RESOLVED.value)
        self.assertTrue((self.state / "adrs" / "ADR-ADR-PENDING-001.json").is_file())
        after_d2 = orchestrator.decide("ADR-PENDING-002", answer="SecurityIdentity remains outside v0.1 Core", rationale="defer until a separate Contract revision")
        self.assertEqual(after_d2.status, "RUNNING")
        self.assertEqual(after_d2.current_task, "TASK-002")
        self.assertEqual(after_d2.work_items["TASK-002"].status, WorkItemStatus.RUNNING.value)

    def test_resume_after_decisions_does_not_rerun_completed_unrelated_work(self):
        config = self.scoped_fixture()
        runner = MockRunner(config.runner.mock_outcomes)
        orchestrator = Orchestrator(config, runner=runner)
        orchestrator.run()
        completed_calls = runner.task_calls.count("TASK-X")
        orchestrator.decide("ADR-PENDING-001", option="defer")
        orchestrator.decide("ADR-PENDING-002", option="defer")
        final = orchestrator.run()
        self.assertEqual(final.status, "COMPLETED")
        self.assertEqual(runner.task_calls.count("TASK-X"), completed_calls)
        self.assertEqual(final.work_items["TASK-X"].status, WorkItemStatus.COMPLETED.value)

    def test_exact_resolved_adr_is_reused_but_new_question_creates_decision(self):
        config = self.scoped_fixture()
        orchestrator = Orchestrator(config, runner=MockRunner(config.runner.mock_outcomes))
        state = orchestrator.run()
        original = state.decisions["ADR-PENDING-001"]
        state = orchestrator.decide("ADR-PENDING-001", option="defer")
        state, unresolved = orchestrator._record_decisions(state, {
            "verdict": Verdict.ARCHITECTURE_DECISION_REQUIRED.value,
            "decision_requests": [original.to_dict()],
        })
        self.assertEqual(unresolved, [])
        state, unresolved = orchestrator._record_decisions(state, {
            "verdict": Verdict.ARCHITECTURE_DECISION_REQUIRED.value,
            "decision_requests": [{
                "decision_id": "ADR-PENDING-NEW",
                "question": "A new architecture question",
                "directly_blocked_items": ["TASK-006"],
            }],
        })
        self.assertEqual(unresolved, ["ADR-PENDING-NEW"])
        self.assertEqual(state.decisions["ADR-PENDING-NEW"].status, DecisionStatus.PENDING.value)

    def test_status_report_exposes_work_counts_and_pending_decisions(self):
        config = self.scoped_fixture()
        orchestrator = Orchestrator(config, runner=MockRunner(config.runner.mock_outcomes))
        orchestrator.run()
        report = orchestrator.status_report()
        self.assertEqual(report["workflow"], "WAITING_HUMAN")
        self.assertEqual(report["work"][WorkItemStatus.BLOCKED_BY_HUMAN_DECISION.value], 4)
        self.assertEqual(report["work"][WorkItemStatus.WAITING_DEPENDENCY.value], 1)
        self.assertEqual(len(report["pending_decisions"]), 2)
        self.assertFalse(report["unaffected_work_continues"])

    def test_pending_decision_keeps_workflow_running_while_ready_branch_exists(self):
        config = self.scoped_fixture()
        runner = MockRunner(config.runner.mock_outcomes)
        orchestrator = Orchestrator(config, runner=runner)
        self.assertEqual(orchestrator.step().state.current_task, "TASK-002")
        continued = orchestrator.step().state
        self.assertEqual(continued.status, "RUNNING")
        self.assertEqual(continued.current_task, "TASK-X")
        self.assertEqual(len([item for item in continued.decisions.values() if item.status == DecisionStatus.PENDING.value]), 2)

    def test_decide_rejects_unknown_option_and_missing_answer(self):
        config = self.scoped_fixture()
        orchestrator = Orchestrator(config, runner=MockRunner(config.runner.mock_outcomes))
        orchestrator.run()
        with self.assertRaises(OrchestratorError):
            orchestrator.decide("ADR-PENDING-001")
        with self.assertRaises(OrchestratorError):
            orchestrator.decide("ADR-PENDING-001", option="not-declared")

    def test_security_and_destructive_verdicts_remain_global_hard_stops(self):
        config = self.workflow("autonomous")
        state = WorkflowState(project="test-project", current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t")
        for verdict in (Verdict.SECURITY_SENSITIVE_ACTION.value, Verdict.DESTRUCTIVE_ACTION_REQUIRED.value, Verdict.FROZEN_SOURCE_MISMATCH.value):
            with self.subTest(verdict=verdict):
                result = transition_after_outcome(config, state, make_outcome("r", state.current_stage, "p", verdict))
                self.assertEqual(result.state.current_stage, Stage.HARD_STOP.value)

    def test_prompt_includes_task_requirements_and_decision_contract(self):
        config = self.scoped_fixture()
        state = WorkflowState(project=config.project_name, current_stage=Stage.TASK_EXECUTION.value, current_group="pais-derived-graph", current_task="TASK-002")
        prompt = PromptBuilder().build(config, state, self.root / "run" / "outcome.json")
        self.assertIn("REQ-MANIFEST-001", prompt)
        self.assertIn("§4", prompt)
        self.assertIn("decision_requests", prompt)


if __name__ == "__main__":
    unittest.main()
