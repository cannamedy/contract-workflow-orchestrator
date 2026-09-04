from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import WorkflowState
from contract_workflow.orchestrator import Orchestrator, OrchestratorError
from contract_workflow.review_evidence import ReviewFindingError, finding_identity
from contract_workflow.state_store import StateStore


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        control = self.project / ".contract-workflow"
        control.mkdir()
        workflow = control / "workflow.yaml"
        workflow.write_text(
            f'''version: "1"
project:
  name: evidence-fixture
  path: {self.project}
mode: autonomous
authoritative_sources: []
skills: {{}}
runner:
  type: mock
groups:
  - id: implementation
    tasks:
      - id: TASK-002
''',
            encoding="utf-8",
        )
        self.config = load_workflow(workflow, self.project)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)
        self.store = StateStore(self.state_root)
        self.orchestrator = Orchestrator(self.config, store=self.store)

    def tearDown(self) -> None:
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def test_historical_finding_is_canonical_persisted_and_idempotent(self) -> None:
        text = "capabilities / frames scalar values were incorrectly converted to tuple"
        first = self.orchestrator.migrate_review_finding(
            task_id="TASK-002", text=text, source_context="historical independent review"
        )
        second = self.orchestrator.migrate_review_finding(
            task_id="TASK-002", text=text, source_context="historical independent review"
        )
        self.assertEqual(first["status"], "CREATED")
        self.assertEqual(second["status"], "ALREADY_REGISTERED")
        self.assertEqual(first["finding"]["finding_id"], second["finding"]["finding_id"])
        state = self.store.load()
        self.assertIsNotNone(state)
        self.assertEqual(len(state.review_findings), 1)
        finding = next(iter(state.review_findings.values()))
        self.assertEqual(finding.status, "UNRESOLVED")
        self.assertEqual(finding.provenance, "HISTORICAL_REVIEW_MIGRATION")
        self.assertEqual(finding.migration_evidence["type"], "MIGRATED_HISTORICAL_REVIEW_EVIDENCE")
        self.assertEqual(state.decisions, {})
        self.assertEqual(state.authority_changes, {})
        self.assertEqual(finding.registered_at, first["finding"]["registered_at"])

    def test_same_id_with_changed_content_is_conflict(self) -> None:
        finding_id = finding_identity("evidence-fixture", "TASK-002", "TASK-002", "original")
        self.orchestrator.migrate_review_finding(task_id="TASK-002", text="original", finding_id=finding_id)
        with self.assertRaises(ReviewFindingError) as error:
            self.orchestrator.migrate_review_finding(task_id="TASK-002", text="changed", finding_id=finding_id)
        self.assertIn("REVIEW_FINDING_CONFLICT", str(error.exception))

    def test_invalid_task_and_empty_finding_are_deterministic_failures(self) -> None:
        with self.assertRaises(ReviewFindingError):
            self.orchestrator.migrate_review_finding(task_id="TASK-404", text="finding")
        with self.assertRaises(ReviewFindingError):
            self.orchestrator.migrate_review_finding(task_id="TASK-002", text=" ")

    def test_canonical_finding_is_carried_forward_and_can_be_resolved(self) -> None:
        text = "capabilities / frames scalar values were incorrectly converted to tuple"
        result = self.orchestrator.migrate_review_finding(task_id="TASK-002", text=text)
        finding_id = result["finding"]["finding_id"]
        state = self.store.load()
        carried = self.orchestrator._carry_forward_task_review_findings(
            state, {"tasks": [{"task_id": "TASK-002", "preserved_review_findings": []}]}
        )
        self.assertEqual(carried["tasks"][0]["preserved_review_findings"], [text])
        self.assertEqual(carried["tasks"][0]["preserved_review_finding_ids"], [finding_id])

        resolved = self.orchestrator.resolve_review_finding(finding_id, "fixed and independently reviewed")
        self.assertEqual(resolved["status"], "RESOLVED")
        state = self.store.load()
        self.assertEqual(next(iter(state.review_findings.values())).status, "RESOLVED")
        after_resolution = self.orchestrator._prior_task_review_findings(state, "TASK-002")
        self.assertNotIn(text, after_resolution)

    def test_persisted_evidence_survives_reload_and_legacy_state_compatibility(self) -> None:
        result = self.orchestrator.migrate_review_finding(task_id="TASK-002", text="historical fact")
        reloaded = StateStore(self.state_root).load()
        self.assertEqual(list(reloaded.review_findings), [result["finding"]["finding_id"]])
        legacy = WorkflowState(project="legacy")
        legacy_payload = legacy.to_dict()
        legacy_payload.pop("review_findings")
        restored = WorkflowState.from_dict(legacy_payload)
        self.assertEqual(restored.review_findings, {})

    def test_status_listing_exposes_canonical_finding(self) -> None:
        self.orchestrator.migrate_review_finding(task_id="TASK-002", text="historical fact")
        listed = self.orchestrator.list_review_findings("TASK-002")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].task_id, "TASK-002")


if __name__ == "__main__":
    unittest.main()
