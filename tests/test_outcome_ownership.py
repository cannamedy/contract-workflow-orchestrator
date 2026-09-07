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
from contract_workflow.models import ArtifactStatus, EngineeringArtifact, Stage, Verdict, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.outcome import make_outcome
from contract_workflow.runners.base import RunnerResult, run_times
from contract_workflow.state_store import StateStore
from contract_workflow.workspace import RunWorkspace


class OutcomeRunner:
    def __init__(self, mode: str):
        self.mode = mode

    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env=None) -> RunnerResult:
        started, finished = run_times()
        stdout = run_dir / "stdout.log"
        stderr = run_dir / "stderr.log"
        stdout.write_text("runner output\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        if self.mode in {"missing", "timeout", "host_lost"}:
            (cwd / "contract.json").write_text('{"candidate": true}\n', encoding="utf-8")
        if self.mode == "malformed":
            (run_dir / "outcome.json").write_text("{malformed", encoding="utf-8")
        if self.mode == "raise":
            raise RuntimeError("simulated Agent host failure")
        if self.mode == "scope":
            (cwd / "contract.json").write_text('{"candidate": true}\n', encoding="utf-8")
            (cwd / "unrelated.txt").write_text("not allowed\n", encoding="utf-8")
            outcome = make_outcome(
                env["CWO_RUN_ID"], Stage.ARTIFACT_GENERATION.value, cwd.name, Verdict.APPROVED.value,
                artifact={
                    "id": "contract", "kind": "MACHINE_CONTRACT",
                    "candidate_content": '{"candidate": true}\n',
                    "candidate_hash": hashlib.sha256(b'{"candidate": true}\n').hexdigest(),
                },
            )
            (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        if self.mode == "claim_only":
            content = '{"candidate": true}\n'
            outcome = make_outcome(
                env["CWO_RUN_ID"], Stage.ARTIFACT_GENERATION.value, cwd.name, Verdict.APPROVED.value,
                artifact={
                    "id": "contract", "kind": "MACHINE_CONTRACT",
                    "candidate_path": "contract.json",
                    "candidate_hash": hashlib.sha256(content.encode()).hexdigest(),
                },
            )
            (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        return RunnerResult(
            0 if self.mode not in {"timeout", "host_lost"} else (-1 if self.mode == "host_lost" else 0),
            stdout, stderr, started, finished,
            timed_out=self.mode == "timeout",
            runner_metadata={"host_lost": "true"} if self.mode == "host_lost" else {},
        )


class TimeoutWithRealDriftRunner(OutcomeRunner):
    def __init__(self, project: Path):
        super().__init__("timeout")
        self.project = project

    def run(self, cwd: Path, prompt: str, run_dir: Path, timeout: int, env=None) -> RunnerResult:
        (self.project / "concurrent.txt").write_text("human edit during invocation\n", encoding="utf-8")
        return super().run(cwd, prompt, run_dir, timeout, env)


class OutcomeOwnershipTests(unittest.TestCase):
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

    def config(self, max_attempts: int = 2):
        control = self.project / ".contract-workflow"
        control.mkdir(exist_ok=True)
        path = control / "workflow.yaml"
        path.write_text(
            f'''version: "1"
project:
  name: outcome-fixture
  path: {self.project}
mode: autonomous
authoritative_sources: []
skills: {{}}
runner:
  type: mock
policy:
  max_attempts_per_stage: {max_attempts}
  retry_backoff_seconds: 0
  retry_max_delay_seconds: 0
artifact_pipeline:
  artifacts:
    - id: contract
      kind: MACHINE_CONTRACT
      review_required: false
      accepted_path: contract.json
groups:
  - id: g
    tasks:
      - id: fixture-task
''',
            encoding="utf-8",
        )
        return load_workflow(path, self.project)

    def invoke(self, mode: str, max_attempts: int = 2):
        config = self.config(max_attempts)
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store, runner=OutcomeRunner(mode))
        initialized = orchestrator.step().state
        if initialized.current_stage == Stage.HARD_STOP.value:
            return orchestrator, initialized, store
        self.assertEqual(initialized.current_stage, Stage.ARTIFACT_GENERATION.value)
        return orchestrator, orchestrator.step().state, store

    def test_missing_result_after_candidate_change_is_canonical_failure(self):
        _, state, store = self.invoke("missing")
        run_id = state.last_outcome["run_id"]
        outcome = json.loads((store.run_dir(run_id) / "outcome.json").read_text())
        self.assertEqual(outcome["verdict"], Verdict.INVALID_OUTCOME.value)
        self.assertEqual(outcome["execution_failure"]["classification"], "AGENT_RESULT_MISSING")
        self.assertEqual(outcome["execution_failure"]["owner"], "CWO_RUNTIME")
        self.assertIsNone(state.artifacts["contract"].candidate_hash)
        self.assertFalse((self.project / "contract.json").exists())

    def test_timeout_and_host_loss_are_canonical_failures(self):
        for mode, expected in (("timeout", "AGENT_TIMEOUT"), ("host_lost", "AGENT_HOST_LOST")):
            with self.subTest(mode=mode):
                os.environ["CWO_STATE_DIR"] = str(self.root / f"state-{mode}")
                self.state_root = self.root / f"state-{mode}"
                _, state, store = self.invoke(mode, max_attempts=1)
                outcome = json.loads((store.run_dir(state.run_id) / "outcome.json").read_text())
                self.assertEqual(outcome["execution_failure"]["classification"], expected)
                self.assertEqual(state.current_stage, Stage.HARD_STOP.value)

    def test_malformed_result_is_preserved_and_canonicalized(self):
        _, state, store = self.invoke("malformed", max_attempts=1)
        run_dir = store.run_dir(state.run_id)
        outcome = json.loads((run_dir / "outcome.json").read_text())
        self.assertEqual(outcome["execution_failure"]["classification"], "AGENT_RESULT_MALFORMED")
        self.assertTrue((run_dir / "agent-outcome.raw.json").is_file())

    def test_runner_exception_has_canonical_failure_and_recoverable_attempt(self):
        _, state, store = self.invoke("raise", max_attempts=1)
        run_dir = store.run_dir(state.run_id)
        outcome = json.loads((run_dir / "outcome.json").read_text())
        metadata = json.loads((run_dir / "metadata.json").read_text())
        self.assertEqual(outcome["execution_failure"]["classification"], "AGENT_PROCESS_FAILED")
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["exit_code"], -1)
        self.assertTrue(state.recoverable)
        recovered = Orchestrator(self.config(1), store=store, runner=OutcomeRunner("missing")).recover()
        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_GENERATION.value)
        self.assertEqual(recovered.status, "RUNNING")

    def test_scope_violation_records_failure_and_does_not_adopt(self):
        _, state, store = self.invoke("scope")
        run_id = state.run_id or state.last_outcome["run_id"]
        outcome = json.loads((store.run_dir(run_id) / "outcome.json").read_text())
        self.assertEqual(state.stop_code, "WORKSPACE_MUTATION_VIOLATION")
        self.assertEqual(outcome["execution_failure"]["classification"], "WORKSPACE_MUTATION_VIOLATION")
        self.assertIsNone(state.artifacts["contract"].candidate_hash)
        self.assertFalse((self.project / "contract.json").exists())
        self.assertFalse((self.project / "unrelated.txt").exists())

    def test_candidate_claim_without_content_or_workspace_change_is_failure(self):
        _, state, store = self.invoke("claim_only", max_attempts=1)
        run_id = state.run_id
        outcome = json.loads((store.run_dir(run_id) / "outcome.json").read_text())
        self.assertEqual(state.stop_code, "AGENT_RESULT_MISSING")
        self.assertEqual(outcome["execution_failure"]["classification"], "AGENT_RESULT_MISSING")
        self.assertIsNone(state.artifacts["contract"].candidate_hash)

    def test_external_candidate_path_is_recovered_only_from_matching_store_content(self):
        config = self.config(max_attempts=1)
        store = StateStore(self.state_root)
        content = '{"candidate": true}\n'
        candidate = store.save_artifact_candidate("contract", content)
        candidate_hash = hashlib.sha256(content.encode()).hexdigest()
        artifacts = initialize_artifacts(config)
        artifacts["contract"] = EngineeringArtifact(
            "contract", "MACHINE_CONTRACT", ArtifactStatus.CANDIDATE.value,
            candidate_hash=candidate_hash, candidate_path="contract.json",
            accepted_path="contract.json",
        )
        store.save(WorkflowState(
            project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
            workflow_digest=config.digest, current_stage=Stage.ARTIFACT_VALIDATION.value,
            current_artifact_id="contract", artifacts=artifacts,
        ))
        loaded = Orchestrator(config, store=store).status()
        self.assertEqual(loaded.artifacts["contract"].candidate_path, str(candidate))
        self.assertEqual(hashlib.sha256(Path(loaded.artifacts["contract"].candidate_path).read_bytes()).hexdigest(), candidate_hash)

    def test_historical_missing_outcome_recovery_is_safe_and_idempotent(self):
        config = self.config()
        store = StateStore(self.state_root)
        snapshot = self.state_root / "authority" / "accepted.md"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("accepted authority\n", encoding="utf-8")
        workspace = RunWorkspace.create(self.project, self.state_root, "historical")
        metadata = {
            "run_id": "historical", "stage": Stage.ARTIFACT_PATCH.value, "status": "completed",
            "exit_code": 0, "timed_out": False, "workspace_path": str(workspace.path),
            "workspace_baseline": workspace.baseline, "real_baseline": workspace.real_baseline,
            "excluded_roots": [str(item) for item in workspace.excluded_roots],
            "workspace_diff_count": 0,
            "authority_materializations": [{"path": "guide.md", "snapshot_path": str(snapshot), "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}],
        }
        run_dir = store.run_dir("historical")
        (run_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        artifacts = initialize_artifacts(config)
        artifacts["spec"] = EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.ACCEPTED.value, accepted_hash="spec-hash")
        artifacts["contract"] = EngineeringArtifact(
            "contract", "MACHINE_CONTRACT", ArtifactStatus.REQUIRES_PATCH.value,
            candidate_hash="old-candidate", accepted_path="contract.json",
            metadata={"dependency_revisions": [{"artifact_id": "spec", "hash": "spec-hash"}]},
        )
        state = WorkflowState(
            project=config.project_name, project_path=config.project_path, workflow_file=config.workflow_file,
            workflow_digest=config.digest, current_stage=Stage.HARD_STOP.value,
            blocked_stage=Stage.ARTIFACT_PATCH.value, current_artifact_id="contract", run_id="historical",
            attempt=3, total_steps=4, stop_code="RETRY_EXHAUSTED", stop_reason="outcome.json is missing",
            status="HARD_STOPPED", artifacts=artifacts,
        )
        store.save(state)
        recovered = Orchestrator(config, store=store, runner=OutcomeRunner("missing")).recover()
        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertIsNone(recovered.run_id)
        self.assertTrue((run_dir / "recovery.json").is_file())
        self.assertFalse((run_dir / "outcome.json").exists())
        repeated = Orchestrator(config, store=store, runner=OutcomeRunner("missing")).recover()
        self.assertEqual(repeated.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(repeated.status, "RUNNING")

    def test_runner_timeout_with_unrelated_real_drift_recovers_and_records_classification(self):
        config = self.config(max_attempts=1)
        store = StateStore(self.state_root)
        runner = TimeoutWithRealDriftRunner(self.project)
        orchestrator = Orchestrator(config, store=store, runner=runner)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.ARTIFACT_GENERATION.value)
        stopped = orchestrator.step().state
        self.assertEqual(stopped.stop_code, "RETRY_EXHAUSTED")
        self.assertTrue(stopped.recoverable)
        recovered = orchestrator.recover()
        self.assertEqual(recovered.current_stage, Stage.ARTIFACT_GENERATION.value)
        self.assertEqual(recovered.status, "RUNNING")
        self.assertIsNone(recovered.run_id)
        drift_files = list(store.runs_path.glob("*/real-drift.json"))
        self.assertTrue(drift_files)
        drift = json.loads(drift_files[-1].read_text(encoding="utf-8"))
        self.assertEqual(drift[0]["path"], "concurrent.txt")
        self.assertEqual(drift[0]["classification"], "UNRELATED_CONCURRENT_DRIFT")
        self.assertEqual((self.project / "concurrent.txt").read_text(encoding="utf-8"), "human edit during invocation\n")


if __name__ == "__main__":
    unittest.main()
