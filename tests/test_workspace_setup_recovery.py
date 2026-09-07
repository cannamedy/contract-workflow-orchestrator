from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.artifacts import initialize_artifacts
from contract_workflow.config import load_workflow
from contract_workflow.models import ArtifactStatus, EngineeringArtifact, Stage, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.state_store import StateStore


class WorkspaceSetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "guide.md").write_text("guide\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)

    def tearDown(self):
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def config(self):
        control = self.project / ".contract-workflow"
        control.mkdir()
        workflow = control / "workflow.yaml"
        workflow.write_text(
            f'''version: "1"
project:
  name: setup-recovery-fixture
  path: {self.project}
mode: autonomous
authoritative_sources: []
skills: {{}}
runner:
  type: mock
policy:
  max_attempts_per_stage: 2
  retry_backoff_seconds: 0
  retry_max_delay_seconds: 0
artifact_pipeline:
  artifacts:
    - id: contract
      kind: MACHINE_CONTRACT
      accepted_path: contract.json
      review_required: true
groups:
  - id: g
    tasks:
      - id: task
''',
            encoding="utf-8",
        )
        return load_workflow(workflow, self.project)

    def test_snapshot_race_stop_recovers_only_matching_durable_candidate(self):
        config = self.config()
        store = StateStore(self.state_root)
        content = '{"candidate": true}\n'
        candidate = store.save_artifact_candidate("contract", content)
        candidate_hash = hashlib.sha256(content.encode()).hexdigest()
        run_id = "setup-race"
        run_dir = store.run_dir(run_id)
        (run_dir / "metadata.json").write_text(json.dumps({
            "run_id": run_id,
            "stage": Stage.ARTIFACT_PATCH.value,
            "status": "failed",
            "error": "real project changed while creating run workspace",
        }), encoding="utf-8")
        artifacts = initialize_artifacts(config)
        artifacts["contract"] = EngineeringArtifact(
            "contract", "MACHINE_CONTRACT", ArtifactStatus.REQUIRES_PATCH.value,
            candidate_hash=candidate_hash, candidate_path=str(candidate), accepted_path="contract.json",
        )
        store.save(WorkflowState(
            project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
            workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value,
            blocked_stage=Stage.ARTIFACT_PATCH.value, current_artifact_id="contract", run_id=run_id,
            stop_code="WORKSPACE_SETUP_FAILED", stop_reason="could not create isolated Agent workspace: real project changed while creating run workspace",
            status="HARD_STOPPED", artifacts=artifacts, current_authority_change_id="CR-1",
            authority_changes={"CR-1": {"change_id": "CR-1", "candidate_sha256": "authority"}},
        ))

        recovered = Orchestrator(config, store=store).recover()

        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertIsNone(recovered.run_id)
        self.assertEqual(recovered.artifacts["contract"].candidate_path, str(candidate))
        repeated = Orchestrator(config, store=store).recover()
        self.assertEqual(repeated.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(repeated.artifacts["contract"].candidate_hash, candidate_hash)


if __name__ == "__main__":
    unittest.main()
