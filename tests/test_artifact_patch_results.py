from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import ArtifactStatus, EngineeringArtifact, Stage, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.prompt_builder import PromptBuilder
from contract_workflow.state_store import StateStore
from contract_workflow.workspace import RunWorkspace


class ArtifactPatchResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "guide.md").write_text("guide\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(
            [
                "git", "-C", str(self.project), "-c", "user.name=CWO",
                "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture",
            ],
            check=True,
        )
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)
        control = self.project / ".contract-workflow"
        control.mkdir()
        self.workflow = control / "workflow.yaml"
        self.workflow.write_text(
            f'''version: "1"
project:
  name: patch-result-fixture
  path: {self.project}
mode: autonomous
authoritative_sources: []
skills: {{}}
runner:
  type: mock
policy:
  max_attempts_per_stage: 1
  retry_backoff_seconds: 0
  retry_max_delay_seconds: 0
artifact_pipeline:
  artifacts:
    - id: contract
      kind: MACHINE_CONTRACT
      review_required: true
      validator_role: validator
      accepted_path: contract.json
groups:
  - id: g
    tasks:
      - id: fixture-task
''',
            encoding="utf-8",
        )
        self.config = load_workflow(self.workflow, self.project)
        self.store = StateStore(self.state_root)

    def tearDown(self) -> None:
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def patch_state(self, content: str = "old\n") -> tuple[WorkflowState, Path]:
        candidate = self.store.save_artifact_candidate("contract", content)
        artifact = EngineeringArtifact(
            "contract",
            "MACHINE_CONTRACT",
            ArtifactStatus.REQUIRES_PATCH.value,
            candidate_hash=hashlib.sha256(content.encode()).hexdigest(),
            candidate_path=str(candidate),
            accepted_path="contract.json",
            review_required=True,
            validator_role="validator",
            metadata={
                "patch_context": {
                    "summary": "two findings require reconciliation",
                    "issues": [
                        {"message": "finding zero"},
                        {"message": "finding one"},
                    ],
                }
            },
        )
        return (
            WorkflowState(
                project=self.config.project_name,
                project_path=self.config.project_path,
                workflow_file=self.config.workflow_file,
                workflow_digest=self.config.digest,
                current_stage=Stage.ARTIFACT_PATCH.value,
                current_artifact_id="contract",
                attempt=1,
                artifacts={"contract": artifact},
            ),
            candidate,
        )

    def workspace_metadata(self, run_id: str, candidate: Path) -> tuple[RunWorkspace, dict[str, object]]:
        workspace = RunWorkspace.create(
            self.project,
            self.state_root,
            run_id,
            {"contract.json": candidate},
        )
        return workspace, {
            "run_id": run_id,
            "stage": Stage.ARTIFACT_PATCH.value,
            "status": "completed",
            "exit_code": 0,
            "timed_out": False,
            "workspace_path": str(workspace.path),
            "workspace_baseline": workspace.baseline,
            "real_baseline": workspace.real_baseline,
            "excluded_roots": [str(path) for path in workspace.excluded_roots],
        }

    @staticmethod
    def reconciliation() -> list[dict[str, object]]:
        return [
            {
                "finding_index": index,
                "disposition": "NO_CHANGE_REQUIRED",
                "reasoning": f"finding {index} is already satisfied",
                "evidence": f"deterministic evidence {index}",
            }
            for index in range(2)
        ]

    def outcome(
        self,
        run_id: str,
        *,
        patch_status: str | None,
        candidate_content: str | None = None,
        verdict: str = "APPROVED",
    ) -> dict[str, object]:
        artifact: dict[str, object] = {"id": "contract", "kind": "MACHINE_CONTRACT"}
        if patch_status is not None:
            artifact["patch_result"] = {
                "status": patch_status,
                "reasoning": "explicit semantic result",
                "finding_reconciliation": self.reconciliation(),
            }
        if candidate_content is not None:
            artifact.update(
                candidate_content=candidate_content,
                candidate_hash=hashlib.sha256(candidate_content.encode()).hexdigest(),
            )
        return {
            "schema_version": "1.0",
            "run_id": run_id,
            "stage": Stage.ARTIFACT_PATCH.value,
            "verdict": verdict,
            "blocking": False,
            "project": self.config.project_name,
            "group": None,
            "task": None,
            "issues": [],
            "changed_files": [],
            "tests": [],
            "next_action": "continue",
            "summary": "patch result",
            "artifact": artifact,
        }

    def finalize(self, state: WorkflowState, candidate: Path, outcome: dict[str, object]):
        run_id = str(outcome["run_id"])
        workspace, metadata = self.workspace_metadata(run_id, candidate)
        artifact = outcome["artifact"]
        if isinstance(artifact, dict) and artifact.get("candidate_content") is not None:
            (workspace.path / "contract.json").write_text(str(artifact["candidate_content"]), encoding="utf-8")
        return Orchestrator(self.config, store=self.store)._finalize_agent_outcome(
            state,
            outcome,
            self.store.run_dir(run_id),
            metadata,
        )

    def test_changed_candidate_without_semantic_patch_result_is_execution_failure(self) -> None:
        state, candidate = self.patch_state()
        result = self.finalize(
            state,
            candidate,
            self.outcome("missing-result", patch_status=None, candidate_content="new\n"),
        )
        canonical = json.loads((self.store.run_dir("missing-result") / "outcome.json").read_text())
        self.assertEqual(result.state.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(canonical["execution_failure"]["result"], "EXECUTION_FAILED")
        self.assertEqual(result.state.artifacts["contract"].candidate_hash, hashlib.sha256(b"old\n").hexdigest())

    def test_patch_applied_requires_candidate_change(self) -> None:
        state, candidate = self.patch_state()
        result = self.finalize(
            state,
            candidate,
            self.outcome("unchanged-applied", patch_status="PATCH_APPLIED", candidate_content="old\n"),
        )
        canonical = json.loads((self.store.run_dir("unchanged-applied") / "outcome.json").read_text())
        self.assertEqual(result.state.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(canonical["execution_failure"]["result"], "EXECUTION_FAILED")
        self.assertIn("PATCH_APPLIED requires a changed candidate", canonical["summary"])

    def test_no_patch_needed_requires_complete_reconciliation_and_rereview(self) -> None:
        state, candidate = self.patch_state()
        result = self.finalize(
            state,
            candidate,
            self.outcome("no-patch", patch_status="NO_PATCH_NEEDED"),
        )
        updated = result.state.artifacts["contract"]
        self.assertEqual(result.state.current_stage, Stage.ARTIFACT_REVIEW.value)
        self.assertEqual(updated.status, ArtifactStatus.REVIEW_REQUIRED.value)
        self.assertEqual(updated.candidate_hash, hashlib.sha256(b"old\n").hexdigest())
        self.assertEqual(updated.metadata["patch_result"]["status"], "NO_PATCH_NEEDED")
        self.assertFalse((self.project / "contract.json").exists())

    def test_no_patch_needed_rejects_incomplete_finding_reconciliation(self) -> None:
        state, candidate = self.patch_state()
        outcome = self.outcome("incomplete-no-patch", patch_status="NO_PATCH_NEEDED")
        artifact = outcome["artifact"]
        assert isinstance(artifact, dict)
        patch_result = artifact["patch_result"]
        assert isinstance(patch_result, dict)
        patch_result["finding_reconciliation"] = self.reconciliation()[:1]
        result = self.finalize(state, candidate, outcome)
        canonical = json.loads((self.store.run_dir("incomplete-no-patch") / "outcome.json").read_text())
        self.assertEqual(result.state.current_stage, Stage.HARD_STOP.value)
        self.assertEqual(canonical["execution_failure"]["result"], "EXECUTION_FAILED")
        self.assertIn("reconcile every current patch finding", canonical["summary"])

    def test_patch_blocked_uses_existing_scoped_decision_mechanism(self) -> None:
        state, candidate = self.patch_state()
        outcome = self.outcome(
            "patch-blocked",
            patch_status="PATCH_BLOCKED",
            verdict="ARCHITECTURE_DECISION_REQUIRED",
        )
        outcome["blocking"] = True
        outcome["directly_affected_work"] = ["fixture-task"]
        outcome["blocking_scope"] = {"directly_blocked_items": ["fixture-task"]}
        outcome["decision_requests"] = [
            {
                "decision_id": "ADR-PATCH-BLOCKED",
                "question": "Which authority interpretation applies?",
                "directly_blocked_items": ["fixture-task"],
            }
        ]
        artifact = outcome["artifact"]
        assert isinstance(artifact, dict)
        patch_result = artifact["patch_result"]
        assert isinstance(patch_result, dict)
        patch_result["blocked_by"] = {
            "type": "HUMAN_AUTHORITY",
            "id": "authority-choice",
            "reason": "the upstream authority permits two incompatible interpretations",
        }
        result = self.finalize(state, candidate, outcome)
        self.assertEqual(result.state.artifacts["contract"].status, ArtifactStatus.BLOCKED.value)
        decision = result.state.decisions["ADR-PATCH-BLOCKED"]
        self.assertEqual(decision.source_artifact_id, "contract")
        self.assertEqual(decision.status, "PENDING")

    def test_patch_prompt_names_workspace_path_and_semantic_results(self) -> None:
        state, _ = self.patch_state()
        state.run_id = "prompt-run"
        prompt = PromptBuilder().build(
            self.config,
            state,
            self.store.run_dir("prompt-run") / "outcome.json",
            execution_workspace=self.project,
        )
        self.assertIn("contract.json (isolated workspace candidate only)", prompt)
        self.assertIn("PATCH_APPLIED", prompt)
        self.assertIn("NO_PATCH_NEEDED", prompt)
        self.assertIn("PATCH_BLOCKED", prompt)
        self.assertIn("never access the external CWO artifact store directly", prompt)

    def test_patch_applied_routes_changed_candidate_to_validation(self) -> None:
        state, candidate = self.patch_state()
        result = self.finalize(
            state,
            candidate,
            self.outcome("patch-applied", patch_status="PATCH_APPLIED", candidate_content="new\n"),
        )
        updated = result.state.artifacts["contract"]
        self.assertEqual(result.state.current_stage, Stage.ARTIFACT_VALIDATION.value)
        self.assertEqual(updated.status, ArtifactStatus.CANDIDATE.value)
        self.assertEqual(updated.candidate_hash, hashlib.sha256(b"new\n").hexdigest())
        self.assertEqual(updated.metadata["patch_result"]["status"], "PATCH_APPLIED")

    def test_patch_applied_derives_candidate_identity_from_changed_workspace_content(self) -> None:
        state, candidate = self.patch_state()
        outcome = self.outcome(
            "workspace-patch-applied",
            patch_status="PATCH_APPLIED",
        )
        workspace, metadata = self.workspace_metadata("workspace-patch-applied", candidate)
        (workspace.path / "contract.json").write_text("new from workspace\n", encoding="utf-8")

        result = Orchestrator(self.config, store=self.store)._finalize_agent_outcome(
            state,
            outcome,
            self.store.run_dir("workspace-patch-applied"),
            metadata,
        )

        updated = result.state.artifacts["contract"]
        self.assertEqual(result.state.current_stage, Stage.ARTIFACT_VALIDATION.value)
        self.assertEqual(updated.status, ArtifactStatus.CANDIDATE.value)
        self.assertEqual(
            updated.candidate_hash,
            hashlib.sha256(b"new from workspace\n").hexdigest(),
        )
        self.assertEqual(updated.metadata["patch_result"]["status"], "PATCH_APPLIED")

    def test_recovery_restores_last_cwo_adopted_candidate_not_failed_mutation(self) -> None:
        state, candidate = self.patch_state()
        adopted = "old\n"
        candidate.write_text("failed process mutation\n", encoding="utf-8")

        adopted_run = self.store.run_dir("adopted-run")
        (adopted_run / "metadata.json").write_text(
            json.dumps(
                {
                    "run_id": "adopted-run",
                    "stage": Stage.ARTIFACT_PATCH.value,
                    "status": "completed",
                    "exit_code": 0,
                    "timed_out": False,
                }
            ),
            encoding="utf-8",
        )
        adopted_summary = "last CWO-adopted patch"
        adopted_outcome = self.outcome(
            "adopted-run",
            patch_status="PATCH_APPLIED",
            candidate_content=adopted,
        )
        adopted_outcome["summary"] = adopted_summary
        (adopted_run / "outcome.json").write_text(json.dumps(adopted_outcome), encoding="utf-8")

        failed_workspace, failed_metadata = self.workspace_metadata("failed-run", candidate)
        failed_metadata["workspace_diff_count"] = 0
        (self.store.run_dir("failed-run") / "metadata.json").write_text(
            json.dumps(failed_metadata), encoding="utf-8"
        )
        (self.store.run_dir("failed-run") / "real-drift.json").write_text("[]\n", encoding="utf-8")
        failed_workspace.discard()

        artifact = state.artifacts["contract"]
        artifact.metadata["last_outcome"] = adopted_summary
        stopped = WorkflowState(
            **{
                **state.__dict__,
                "current_stage": Stage.HARD_STOP.value,
                "blocked_stage": Stage.ARTIFACT_PATCH.value,
                "status": "HARD_STOPPED",
                "stop_code": "AGENT_RESULT_MISSING",
                "stop_reason": "artifact candidate content is missing and the workspace has no scoped candidate change",
                "run_id": "failed-run",
                "recoverable": False,
            }
        )
        self.store.save(stopped)

        recovered = Orchestrator(self.config, store=self.store).recover()
        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertEqual(candidate.read_text(encoding="utf-8"), adopted)
        recovery = list((self.store.root / "recovery-events").glob("artifact-patch-result-*.json"))
        self.assertEqual(len(recovery), 1)
        evidence = json.loads(recovery[0].read_text(encoding="utf-8"))
        self.assertEqual(evidence["source_run_id"], "adopted-run")
        self.assertFalse(evidence["failed_workspace_adopted"])


if __name__ == "__main__":
    unittest.main()
