from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from contract_workflow.config import load_workflow
from contract_workflow.authority_set import aggregate_authority_set_hash
from contract_workflow.models import ArtifactStatus, DecisionStatus, EngineeringArtifact, HumanDecision, Stage, WorkflowState, WorkflowStatus
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.state_store import StateStore


class TypedAuthorityPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "guide.md").write_text("R1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)

    def tearDown(self) -> None:
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def _config(self):
        control = self.project / ".contract-workflow"
        control.mkdir()
        workflow = control / "workflow.yaml"
        old_hash = hashlib.sha256(b"R1\n").hexdigest()
        lines = [
            'version: "1"', 'project:', '  name: typed-fixture', f'  path: {self.project}',
            'mode: autonomous', 'authority:', '  remote: origin', '  branch: main',
            'authoritative_sources:', '  - source_id: human-guide', '    role: HUMAN_GUIDE',
            '    path: guide.md', f'    sha256: {old_hash}', 'skills: {}', 'runner:',
            '  type: mock', 'groups:', '  - id: g', '    tasks:', '      - id: TASK-002',
            'artifact_pipeline:', '  artifacts:', '    - id: human-guide', '      kind: HUMAN_GUIDE',
            '      promotion_policy: EXTERNAL', '      review_required: false',
            '    - id: engineering-spec', '      kind: ENGINEERING_SPEC',
            '      dependencies: [human-guide]', '      promotion_policy: AUTO',
            '    - id: implementation-plan', '      kind: IMPLEMENTATION_PLAN',
            '      dependencies: [engineering-spec]', '      promotion_policy: AUTO',
        ]
        workflow.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_workflow(workflow, self.project)

    def _remote_candidate(self, store: StateStore) -> tuple[str, Path]:
        content = b"R2 submitted\n"
        candidate_hash = hashlib.sha256(content).hexdigest()
        snapshot = self.state_root / "authority" / "snapshots" / "r2" / "human-guide.md"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(content)
        store.save_authority_ledger({"schema_version": "1.0", "sources": {"human-guide": {
            "source_id": "human-guide", "role": "HUMAN_GUIDE", "status": "CHANGE_PENDING",
            "path": "guide.md", "configured_path": "guide.md", "accepted_sha256": hashlib.sha256(b"R1\n").hexdigest(),
            "candidate_sha256": candidate_hash, "candidate_content_sha256": candidate_hash,
            "candidate_remote_commit": "commit-r2", "candidate_remote_blob": "blob-r2",
            "candidate_snapshot_path": str(snapshot), "change_id": "CR-1",
        }}})
        store.save_remote_state({"schema_version": "1.0", "sources": {"human-guide": {
            "remote_url": "https://example.invalid/pais.git", "branch": "main",
            "commit_sha": "commit-r2", "git_blob_sha": "blob-r2", "content_sha256": candidate_hash,
            "snapshot_path": str(snapshot),
        }}})
        return candidate_hash, snapshot

    def test_legacy_typed_derivatives_are_migrated_to_human_guide_gate(self):
        config = self._config()
        store = StateStore(self.state_root)
        candidate_hash, snapshot = self._remote_candidate(store)
        old_decision_id = "ADR-AUTHORITY-PROMOTION-CR-1"
        old_decision = HumanDecision(
            decision_id=old_decision_id, category="AUTHORITY_PROMOTION",
            question="Promote the old combined baseline?", source_change="CR-1",
            source_stage=Stage.TASK_REBASE_ANALYSIS.value, affected_tasks=("TASK-002",),
            directly_blocked_items=("TASK-002",),
        )
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path,
            workflow_file=config.workflow_file, workflow_digest=config.digest,
            current_stage=Stage.WAITING_FOR_HUMAN.value, status=WorkflowStatus.WAITING_HUMAN.value,
            current_authority_change_id="CR-1",
            authority_changes={"CR-1": {
                "change_id": "CR-1", "source_id": "human-guide", "source_role": "HUMAN_GUIDE",
                "source_path": "guide.md", "configured_source_path": "guide.md",
                "base_sha256": hashlib.sha256(b"R1\n").hexdigest(), "candidate_sha256": candidate_hash,
                "candidate_commit": "commit-r2", "candidate_blob_sha": "blob-r2",
                "candidate_snapshot_path": str(snapshot), "classification": "C4", "semantic_change": True,
                "directly_affected_artifacts": ["engineering-spec"],
                "affected_artifacts": ["engineering-spec", "implementation-plan"],
                "directly_affected_tasks": ["TASK-002"], "status": "PROPAGATING",
            }},
            decisions={old_decision_id: old_decision},
            propagation={"CR-1": {
                "change_id": "CR-1", "status": "WAITING_PROMOTION",
                "stages": [Stage.CHANGE_PROPAGATION_PLANNING.value, Stage.PLAN_GRAPH_BUILD.value, Stage.TASK_REBASE_ANALYSIS.value],
                "promotion_decision_id": old_decision_id, "plan_graph": {"plan_sha256": "legacy"},
                "task_rebase": {"tasks": [{"task_id": "TASK-002", "preserved_review_findings": []}]},
            }},
        )
        store.save(state)

        migrated = Orchestrator(config, store=store)._load_or_initialize()
        old = migrated.decisions[old_decision_id]
        new = migrated.decisions["ADR-HUMAN-GUIDE-PROMOTION-CR-1"]
        self.assertEqual(old.status, DecisionStatus.SUPERSEDED.value)
        self.assertEqual(old.superseded_by, new.decision_id)
        self.assertEqual(new.source_artifact_id, "human-guide")
        self.assertIn("not approved", new.context)
        self.assertEqual(migrated.propagation["CR-1"]["mode"], "typed")
        self.assertEqual(migrated.propagation["CR-1"]["legacy_derivatives_status"], "SUPERSEDED")
        self.assertIsNone(migrated.plan_graph)
        self.assertEqual(migrated.artifacts["human-guide"].candidate_hash, candidate_hash)
        self.assertEqual(migrated.artifacts["human-guide"].status, "PROMOTION_READY")
        self.assertEqual(set(migrated.artifacts), {"human-guide", "engineering-spec", "implementation-plan"})

    def test_task_rebase_carries_named_prior_findings_additively(self):
        config = self._config()
        store = StateStore(self.state_root)
        finding = "capabilities / frames scalar values were incorrectly converted to tuple"
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path,
            workflow_file=config.workflow_file, workflow_digest=config.digest,
            propagation={"old": {"task_rebase": {"tasks": [{"task_id": "TASK-002", "preserved_review_findings": [finding]}]}}},
        )
        result = Orchestrator(config, store=store)._carry_forward_task_review_findings(
            state, {"tasks": [{"task_id": "TASK-002", "preserved_review_findings": []}]},
        )
        self.assertEqual(result["tasks"][0]["preserved_review_findings"], [finding])

    def test_nonsemantic_authority_revision_preserves_typed_artifacts_and_resumes_validation(self):
        config = self._config()
        store = StateStore(self.state_root)
        old_hash = hashlib.sha256(b"R1\n").hexdigest()
        candidate_hash, snapshot = self._remote_candidate(store)
        member = {
            "member_id": "architecture-guide", "role": "ARCHITECTURE_GUIDE", "path": "guide.md",
            "content_sha256": candidate_hash, "git_blob_sha": "blob-r2",
            "snapshot_path": str(snapshot), "source_revision": "commit-r2",
        }
        authority_set_hash = aggregate_authority_set_hash([member])
        manifest = snapshot.parent / "authority-set.json"
        manifest.write_text(json.dumps({"aggregate_hash": authority_set_hash, "members": [member]}), encoding="utf-8")
        ledger = store.load_authority_ledger()
        assert ledger is not None
        entry = ledger["sources"]["human-guide"]
        entry.update({
            "accepted_content_sha256": old_hash,
            "accepted_authority_content_sha256": old_hash,
            "accepted_remote_commit": "commit-r1",
            "accepted_remote_blob": "blob-r1",
            "accepted_authority_blob": "blob-r1",
            "accepted_snapshot_path": str(self.project / "guide.md"),
            "candidate_authority_blob": "blob-r2",
        })
        ledger["authority_set"] = {
            "accepted_hash": "b" * 64,
            "candidate_hash": authority_set_hash,
            "candidate_members": [member],
            "candidate_commit": "commit-r2",
            "candidate_revision_id": "AS-REV-2",
            "status": "CHANGE_PENDING",
            "change_id": "CR-1",
        }
        store.save_authority_ledger(ledger)
        engineering = EngineeringArtifact(
            "engineering-spec", "ENGINEERING_SPEC", ArtifactStatus.ACCEPTED.value,
            version_hash="spec-hash", accepted_hash="spec-hash", candidate_hash="spec-hash",
            metadata={"dependency_revisions": [{
                "artifact_id": "human-guide", "status": ArtifactStatus.ACCEPTED.value,
                "hash": old_hash, "accepted_hash": old_hash, "candidate_hash": None,
            }]},
        )
        plan = EngineeringArtifact(
            "implementation-plan", "IMPLEMENTATION_PLAN", ArtifactStatus.CANDIDATE.value,
            candidate_hash="plan-hash", metadata={"dependency_revisions": [{
                "artifact_id": "engineering-spec", "status": ArtifactStatus.ACCEPTED.value,
                "hash": "spec-hash", "accepted_hash": "spec-hash", "candidate_hash": "spec-hash",
            }]},
        )
        human = EngineeringArtifact(
            "human-guide", "HUMAN_GUIDE", ArtifactStatus.PROMOTION_READY.value,
            version_hash=candidate_hash, accepted_hash=old_hash, candidate_hash=candidate_hash,
            candidate_path=str(snapshot), promotion_policy="EXTERNAL", change_id="CR-1",
        )
        change = {
            "change_id": "CR-1", "source_id": "human-guide", "source_role": "HUMAN_AUTHORITY_SET",
            "source_path": "guide.md", "configured_source_path": "guide.md",
            "base_sha256": old_hash, "candidate_sha256": candidate_hash,
            "candidate_commit": "commit-r2", "candidate_blob_sha": "blob-r2",
            "candidate_snapshot_path": str(snapshot), "candidate_authority_set_hash": authority_set_hash,
            "candidate_authority_set_snapshot_path": str(manifest), "authority_set_members": [member],
            "candidate_revision_id": "AS-REV-2", "classification": None, "status": "CHANGE_PENDING",
        }
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path,
            workflow_file=config.workflow_file, workflow_digest=config.digest,
            current_stage=Stage.AUTHORITY_CHANGE_ANALYSIS.value, current_artifact_id="implementation-plan",
            current_authority_change_id="CR-1", authority_changes={"CR-1": change},
            artifacts={"human-guide": human, "engineering-spec": engineering, "implementation-plan": plan},
        )
        analysis = {
            "change_id": "CR-1", "base_sha256": old_hash, "candidate_sha256": candidate_hash,
            "classification": "C1", "semantic_change": False, "affected_requirements": [],
            "affected_contract_anchors": [], "directly_affected_tasks": [],
            "dependency_affected_tasks": [], "unaffected_tasks": ["TASK-002"],
            "directly_affected_artifacts": [], "dependency_affected_artifacts": [],
            "machine_resolvable": True, "human_decision_required": False,
            "human_decision_requests": [], "required_propagation": [], "analysis_summary": "editorial only",
        }
        outcome = {"verdict": "APPROVED", "authority_change": analysis}

        with patch("contract_workflow.orchestrator.validate_analysis", return_value=(analysis, [])):
            result = Orchestrator(config, store=store)._apply_authority_analysis(state, outcome)

        updated = result.state
        self.assertEqual(updated.authority_changes["CR-1"]["status"], "ACCEPTED")
        self.assertNotIn("CR-1", updated.propagation)
        self.assertEqual(updated.artifacts["human-guide"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(updated.artifacts["human-guide"].accepted_hash, candidate_hash)
        self.assertEqual(updated.artifacts["engineering-spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(updated.artifacts["engineering-spec"].metadata["dependency_revisions"][0]["hash"], candidate_hash)
        self.assertEqual(updated.artifacts["implementation-plan"].status, ArtifactStatus.CANDIDATE.value)
        self.assertEqual(updated.current_stage, Stage.ARTIFACT_VALIDATION.value)
        self.assertEqual(updated.current_artifact_id, "implementation-plan")
        accepted = store.load_authority_ledger()
        assert accepted is not None
        self.assertEqual(accepted["sources"]["human-guide"]["accepted_content_sha256"], candidate_hash)
        self.assertEqual(accepted["authority_set"]["accepted_hash"], authority_set_hash)
        self.assertEqual((self.project / "guide.md").read_text(encoding="utf-8"), "R1\n")

    def test_recovery_repairs_pre_fix_empty_nonsemantic_propagation_from_artifact_records(self):
        config = self._config()
        store = StateStore(self.state_root)
        old_hash = hashlib.sha256(b"R1\n").hexdigest()
        candidate_hash, snapshot = self._remote_candidate(store)
        engineering = EngineeringArtifact(
            "engineering-spec", "ENGINEERING_SPEC", ArtifactStatus.ACCEPTED.value,
            version_hash="spec-hash", accepted_hash="spec-hash", candidate_hash="spec-hash",
            metadata={"dependency_revisions": [{
                "artifact_id": "human-guide", "status": ArtifactStatus.ACCEPTED.value,
                "hash": old_hash, "accepted_hash": old_hash, "candidate_hash": None,
            }]},
        )
        plan = EngineeringArtifact(
            "implementation-plan", "IMPLEMENTATION_PLAN", ArtifactStatus.CANDIDATE.value,
            candidate_hash=hashlib.sha256(b"plan candidate").hexdigest(),
            metadata={"dependency_revisions": [{
                "artifact_id": "engineering-spec", "status": ArtifactStatus.ACCEPTED.value,
                "hash": "spec-hash", "accepted_hash": "spec-hash", "candidate_hash": "spec-hash",
            }]},
        )
        human = EngineeringArtifact("human-guide", "HUMAN_GUIDE", ArtifactStatus.MISSING.value, promotion_policy="EXTERNAL")
        for artifact in (human, engineering, plan):
            store.save_artifact(artifact.to_dict())
        store.save_artifact_candidate("implementation-plan", "plan candidate")
        analysis = {
            "change_id": "CR-1", "base_sha256": old_hash, "candidate_sha256": candidate_hash,
            "classification": "C1", "semantic_change": False, "affected_requirements": [],
            "affected_contract_anchors": [], "directly_affected_tasks": [],
            "dependency_affected_tasks": [], "unaffected_tasks": ["TASK-002"],
            "directly_affected_artifacts": [], "dependency_affected_artifacts": [],
            "affected_artifacts": [], "machine_resolvable": True, "human_decision_required": False,
            "human_decision_requests": [], "required_propagation": [], "analysis_summary": "editorial only",
        }
        change = {
            **analysis, "source_id": "human-guide", "source_role": "HUMAN_GUIDE", "source_path": "guide.md",
            "configured_source_path": "guide.md", "candidate_commit": "commit-r2", "candidate_blob_sha": "blob-r2",
            "candidate_snapshot_path": str(snapshot), "status": "PROPAGATING",
        }
        broken_engineering = EngineeringArtifact(**{**engineering.to_dict(), "status": ArtifactStatus.PENDING.value})
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path,
            workflow_file=config.workflow_file, workflow_digest=config.digest,
            current_stage=Stage.HARD_STOP.value, blocked_stage=Stage.ARTIFACT_GENERATION.value,
            stop_code="MAX_TOTAL_STEPS", stop_reason="max_total_steps exceeded", status=WorkflowStatus.HARD_STOPPED.value,
            current_artifact_id="human-guide", current_authority_change_id="CR-1", total_steps=50,
            last_outcome={"verdict": "APPROVED", "authority_change": analysis},
            authority_changes={"CR-1": change},
            propagation={"CR-1": {"change_id": "CR-1", "status": "RUNNING", "artifact_order": [], "stages": []}},
            artifacts={"human-guide": human, "engineering-spec": broken_engineering, "implementation-plan": plan},
        )
        store.save(state)

        recovered = Orchestrator(config, store=store).recover()

        self.assertEqual(recovered.status, WorkflowStatus.RUNNING.value)
        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_VALIDATION.value)
        self.assertEqual(recovered.current_artifact_id, "implementation-plan")
        self.assertEqual(recovered.authority_changes["CR-1"]["status"], "ACCEPTED")
        self.assertNotIn("CR-1", recovered.propagation)
        self.assertEqual(recovered.artifacts["human-guide"].accepted_hash, candidate_hash)
        self.assertEqual(recovered.artifacts["engineering-spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(recovered.artifacts["engineering-spec"].metadata["dependency_revisions"][0]["hash"], candidate_hash)
        self.assertTrue((self.state_root / "recovery-events" / "nonsemantic-authority-CR-1.json").is_file())


if __name__ == "__main__":
    unittest.main()
