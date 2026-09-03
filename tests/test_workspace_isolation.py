from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import Stage, Verdict, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.outcome import make_outcome
from contract_workflow.runners.base import RunnerResult, run_times
from contract_workflow.state_store import StateStore
from contract_workflow.workspace import RunWorkspace, apply_validated_diff


class WorkspaceRunner:
    def __init__(self, behavior, real_project: Path):
        self.behavior = behavior
        self.real_project = real_project

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        stage = next(line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("CURRENT STAGE:"))
        self.behavior(cwd, self.real_project)
        outcome = make_outcome(env["CWO_RUN_ID"], stage, cwd.name, Verdict.APPROVED.value)
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class AnalysisMutationRunner:
    def __init__(self, store: StateStore):
        self.store = store

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        (cwd / "guide.md").write_text("shadow mutation\n", encoding="utf-8")
        state = self.store.load()
        change = state.authority_changes[state.current_authority_change_id]
        authority_change = {
            "change_id": change["change_id"],
            "base_sha256": change["base_sha256"],
            "candidate_sha256": change["candidate_sha256"],
            "classification": "C2",
            "semantic_change": True,
            "affected_requirements": ["REQ-A"],
            "affected_contract_anchors": [],
            "directly_affected_tasks": ["t"],
            "dependency_affected_tasks": [],
            "unaffected_tasks": [],
            "machine_resolvable": True,
            "human_decision_required": False,
            "human_decision_requests": [],
            "required_propagation": ["CONTRACT_REVISION_REQUIRED"],
            "analysis_summary": "shadow workspace mutation test",
        }
        outcome = make_outcome(env["CWO_RUN_ID"], Stage.AUTHORITY_CHANGE_ANALYSIS.value, state.project, "APPROVED", authority_change=authority_change)
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class WorkspaceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "src").mkdir()
        (self.project / "src" / "a.py").write_text("original\n", encoding="utf-8")
        (self.project / "guide.md").write_text("accepted\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.state = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state)

    def tearDown(self):
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def config(self, *, authority: bool = False, authority_mutable: bool = False, broad_scope: bool = False):
        guide_sha = hashlib.sha256((self.project / "guide.md").read_bytes()).hexdigest()
        allowed_path = '"**"' if broad_scope else "src/a.py"
        lines = [
            'version: "1"', 'project:', '  name: workspace-fixture', f'  path: {self.project}',
            'mode: autonomous', 'authoritative_sources:',
        ]
        if authority:
            lines.extend(['  - source_id: human-guide', '    role: HUMAN_GUIDE', '    path: guide.md', f'    sha256: {guide_sha}'])
            if authority_mutable:
                lines.append('    mutable_after_start: true')
        lines.extend([
            'skills: {}', 'runner:', '  type: mock', 'policy:',
            '  max_attempts_per_stage: 2', '  max_total_steps: 30', 'groups:',
            '  - id: g', '    tasks:', '      - id: t', f'        allowed_paths: [{allowed_path}]',
        ])
        control = self.project / ".contract-workflow"
        control.mkdir(exist_ok=True)
        path = control / "workflow.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_workflow(path, self.project)

    def orchestrator(self, runner, *, authority=False):
        config = self.config(authority=authority)
        return Orchestrator(config, store=StateStore(self.state), runner=runner)

    def start_task(self, runner):
        orchestrator = self.orchestrator(runner)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        return orchestrator

    def test_allowed_task_patch_is_applied_and_workspace_discarded(self):
        runner = WorkspaceRunner(lambda cwd, real: (cwd / "src" / "a.py").write_text("patched\n", encoding="utf-8"), self.project)
        result = self.start_task(runner).step().state
        self.assertEqual(result.current_stage, Stage.TASK_INDEPENDENT_REVIEW.value)
        self.assertEqual((self.project / "src" / "a.py").read_text(), "patched\n")
        self.assertFalse(list((self.state / "workspaces").glob("*/project")))

    def test_preexisting_dirty_allowed_target_is_a_safe_commit_back_baseline(self):
        (self.project / "src" / "a.py").write_text("user-baseline\n", encoding="utf-8")
        runner = WorkspaceRunner(lambda cwd, real: (cwd / "src" / "a.py").write_text("patched\n", encoding="utf-8"), self.project)
        result = self.start_task(runner).step().state
        self.assertEqual(result.current_stage, Stage.TASK_INDEPENDENT_REVIEW.value)
        self.assertEqual((self.project / "src" / "a.py").read_text(), "patched\n")

    def test_agent_change_to_preexisting_unrelated_file_is_rejected(self):
        unrelated = self.project / "user-notes.txt"
        unrelated.write_text("baseline\n", encoding="utf-8")
        def mutate(cwd, real):
            (cwd / "src" / "a.py").write_text("patched\n", encoding="utf-8")
            (cwd / "user-notes.txt").write_text("agent\n", encoding="utf-8")
        stopped = self.start_task(WorkspaceRunner(mutate, self.project)).step().state
        self.assertEqual(stopped.stop_code, "UNAUTHORIZED_WORKSPACE_MUTATION")
        self.assertEqual(unrelated.read_text(), "baseline\n")

    def test_allowed_plus_authority_change_is_rejected_transactionally(self):
        def mutate(cwd, real):
            (cwd / "src" / "a.py").write_text("patched\n", encoding="utf-8")
            (cwd / "guide.md").write_text("authority\n", encoding="utf-8")
        stopped = self.start_task(WorkspaceRunner(mutate, self.project)).step().state
        self.assertEqual(stopped.stop_code, "UNAUTHORIZED_WORKSPACE_MUTATION")
        self.assertEqual((self.project / "src" / "a.py").read_text(), "original\n")
        self.assertEqual((self.project / "guide.md").read_text(), "accepted\n")

    def test_authority_is_protected_even_when_task_scope_is_broad(self):
        config = self.config(authority=True, authority_mutable=True, broad_scope=True)
        runner = WorkspaceRunner(lambda cwd, real: (cwd / "guide.md").write_text("shadow\n", encoding="utf-8"), self.project)
        orchestrator = Orchestrator(config, store=StateStore(self.state), runner=runner)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        stopped = orchestrator.step().state
        self.assertEqual(stopped.stop_code, "UNAUTHORIZED_AUTHORITY_MUTATION")
        self.assertEqual((self.project / "guide.md").read_text(), "accepted\n")

    def test_delete_and_rename_authority_are_rejected_without_real_mutation(self):
        for operation in ("delete", "rename"):
            with self.subTest(operation=operation):
                self.state = self.root / f"state-{operation}"
                os.environ["CWO_STATE_DIR"] = str(self.state)
                def mutate(cwd, real, operation=operation):
                    if operation == "delete":
                        (cwd / "guide.md").unlink()
                    else:
                        (cwd / "guide.md").rename(cwd / "renamed-guide.md")
                stopped = self.start_task(WorkspaceRunner(mutate, self.project)).step().state
                self.assertEqual(stopped.stop_code, "UNAUTHORIZED_WORKSPACE_MUTATION")
                self.assertEqual((self.project / "guide.md").read_text(), "accepted\n")

    def test_target_drift_during_agent_run_refuses_commit_back(self):
        def mutate(cwd, real):
            (cwd / "src" / "a.py").write_text("agent\n", encoding="utf-8")
            (real / "src" / "a.py").write_text("user-change\n", encoding="utf-8")
        stopped = self.start_task(WorkspaceRunner(mutate, self.project)).step().state
        self.assertEqual(stopped.stop_code, "REAL_PROJECT_CHANGED_DURING_RUN")
        self.assertEqual((self.project / "src" / "a.py").read_text(), "user-change\n")

    def test_strict_analysis_mutation_is_discarded_and_real_candidate_is_unchanged(self):
        config = self.config(authority=True)
        store = StateStore(self.state)
        orchestrator = Orchestrator(config, store=store)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.TASK_EXECUTION.value)
        (self.project / "guide.md").write_text("candidate\n", encoding="utf-8")
        self.assertEqual(orchestrator.step().action, "authority_change_analysis")
        stopped = Orchestrator(config, store=store, runner=AnalysisMutationRunner(store)).step().state
        self.assertEqual(stopped.stop_code, "WORKSPACE_MUTATION_VIOLATION")
        self.assertEqual((self.project / "guide.md").read_text(), "candidate\n")
        self.assertFalse(list((self.state / "workspaces").glob("*/project")))

    def test_snapshot_contains_current_untracked_view_and_excludes_git(self):
        (self.project / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        workspace = RunWorkspace.create(self.project, self.state, "snapshot")
        self.assertTrue((workspace.path / "untracked.txt").is_file())
        self.assertFalse((workspace.path / ".git").exists())
        workspace.discard()

    def test_candidate_workspace_is_externalized_without_touching_accepted_file(self):
        accepted = self.project / "contract.md"
        accepted.write_text("accepted contract\n", encoding="utf-8")
        workspace = RunWorkspace.create(self.project, self.state, "candidate")
        (workspace.path / "contract.md").write_text("candidate contract\n", encoding="utf-8")
        self.assertEqual(len(workspace.diff()), 1)
        self.assertEqual(accepted.read_text(), "accepted contract\n")
        workspace.discard()

    def test_unfinished_workspace_recovery_never_commits(self):
        config = self.config()
        store = StateStore(self.state)
        workspace = RunWorkspace.create(self.project, self.state, "unfinished")
        run_dir = store.run_dir("unfinished")
        metadata = {
            "run_id": "unfinished", "stage": Stage.TASK_EXECUTION.value, "status": "running",
            "workspace_path": str(workspace.path), "workspace_baseline": workspace.baseline,
            "real_baseline": workspace.real_baseline, "excluded_roots": [str(p) for p in workspace.excluded_roots],
        }
        (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        store.save(WorkflowState(project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file, workflow_digest=config.digest, current_stage=Stage.TASK_EXECUTION.value, current_group="g", current_task="t", run_id="unfinished"))
        stopped = Orchestrator(config, store=store).step().state
        self.assertEqual(stopped.stop_code, "RECOVERY_UNCERTAIN")
        self.assertEqual((self.project / "src" / "a.py").read_text(), "original\n")

    def test_validated_diff_requires_unchanged_real_baseline(self):
        workspace = RunWorkspace.create(self.project, self.state, "drift")
        (workspace.path / "src" / "a.py").write_text("agent\n", encoding="utf-8")
        changes = workspace.diff()
        (self.project / "src" / "a.py").write_text("drift\n", encoding="utf-8")
        with self.assertRaises(Exception):
            apply_validated_diff(workspace, changes, ("src/a.py",))
        workspace.discard()


if __name__ == "__main__":
    unittest.main()
