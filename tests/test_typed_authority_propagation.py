from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import DecisionStatus, HumanDecision, Stage, WorkflowState, WorkflowStatus
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


if __name__ == "__main__":
    unittest.main()
