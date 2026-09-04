from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.artifacts import artifact_impact_closure, dependency_revisions, initialize_artifacts, missing_skill_roles, reconcile_artifact_staleness, validate_artifact_graph, validate_artifact_promotion, validate_final_conformance
from contract_workflow.config import WorkflowConfigError, load_workflow
from contract_workflow.models import ArtifactSpec, ArtifactStatus, EngineeringArtifact, Stage, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.outcome import make_outcome
from contract_workflow.runners.base import RunnerResult, run_times
from contract_workflow.state_store import StateStore


class ArtifactRunner:
    def __init__(self, project: str, behavior: dict[str, list[str]] | None = None):
        self.project = project
        self.behavior = behavior or {}
        self.calls: list[tuple[str, str]] = []

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        stage = next(line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("CURRENT STAGE:"))
        artifact_id = next(line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("CURRENT ARTIFACT:"))
        self.calls.append((stage, artifact_id))
        values = self.behavior.get(stage, [])
        verdict = values.pop(0) if values else "APPROVED"
        extra = {"artifact": {"id": artifact_id, "kind": "ENGINEERING_SPEC", "review": {"summary": "independent review"}}}
        if stage in {Stage.ARTIFACT_GENERATION.value, Stage.ARTIFACT_PATCH.value}:
            content = f"candidate-{artifact_id}-{len(self.calls)}\n"
            extra["artifact"] = {"id": artifact_id, "kind": "ENGINEERING_SPEC", "candidate_content": content, "candidate_hash": hashlib.sha256(content.encode()).hexdigest()}
        outcome = make_outcome(env["CWO_RUN_ID"], stage, self.project, verdict, **extra)
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class ArtifactPipelineTests(unittest.TestCase):
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

    def config(self, pipeline: str, skills: str = "{}"):
        control = self.project / ".contract-workflow"
        control.mkdir(exist_ok=True)
        path = control / "workflow.yaml"
        path.write_text(f'''version: "1"\nproject:\n  name: artifact-fixture\n  path: {self.project}\nmode: autonomous\nauthoritative_sources: []\nskills: {skills}\nrunner:\n  type: mock\ngroups:\n  - id: g\n    tasks:\n      - id: coding-task\nartifact_pipeline:\n  artifacts:\n{pipeline}\n''', encoding="utf-8")
        return load_workflow(path, self.project)

    def test_artifact_graph_validates_dag_and_optional_nodes(self):
        specs = (ArtifactSpec("guide", "HUMAN_GUIDE"), ArtifactSpec("spec", "ENGINEERING_SPEC", ("guide",)), ArtifactSpec("optional", "MACHINE_CONTRACT", ("guide",), optional=True, enabled=False))
        self.assertEqual(validate_artifact_graph(specs), [])
        cycle = (ArtifactSpec("a", "HUMAN_GUIDE", ("b",)), ArtifactSpec("b", "ENGINEERING_SPEC", ("a",)))
        self.assertTrue(any("cycle" in item for item in validate_artifact_graph(cycle)))
        config = self.config("    - id: guide\n      kind: HUMAN_GUIDE\n    - id: spec\n      kind: ENGINEERING_SPEC\n      dependencies: [guide]\n")
        self.assertEqual(artifact_impact_closure(config, ["guide"]), {"guide", "spec"})

    def test_generic_generation_review_patch_and_candidate_isolation(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      review_required: true\n")
        runner = ArtifactRunner(config.project_name, {Stage.ARTIFACT_REVIEW.value: ["REQUIRES_PATCH", "APPROVED"]})
        orchestrator = Orchestrator(config, store=StateStore(self.state_root), runner=runner)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.ARTIFACT_GENERATION.value)
        generated = orchestrator.step().state
        self.assertEqual(generated.artifacts["spec"].status, ArtifactStatus.REVIEW_REQUIRED.value)
        self.assertEqual(generated.current_stage, Stage.ARTIFACT_REVIEW.value)
        patched = orchestrator.step().state
        self.assertEqual(patched.artifacts["spec"].status, ArtifactStatus.REQUIRES_PATCH.value)
        self.assertEqual(patched.current_stage, Stage.ARTIFACT_PATCH.value)
        rereview = orchestrator.step().state
        self.assertEqual(rereview.current_stage, Stage.ARTIFACT_REVIEW.value)
        approved = orchestrator.step().state
        self.assertEqual(approved.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertTrue(list((self.state_root / "artifacts" / "spec").glob("candidate")))
        self.assertTrue((self.state_root / "artifacts" / "spec" / "accepted").is_file())
        self.assertTrue((self.state_root / "artifacts" / "spec" / "promotion.json").is_file())

    def test_optional_artifact_can_be_skipped_and_missing_skill_is_diagnostic(self):
        config = self.config("    - id: optional\n      kind: MACHINE_CONTRACT\n      optional: true\n      enabled: false\n      skill_role: machine_contract\n")
        self.assertEqual(missing_skill_roles(config), ["machine_contract"])

    def test_human_gate_reuses_decision_system_for_promotion(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      promotion_policy: HUMAN_GATE\n")
        runner = ArtifactRunner(config.project_name)
        orchestrator = Orchestrator(config, store=StateStore(self.state_root), runner=runner)
        self.assertEqual(orchestrator.step().state.current_stage, Stage.ARTIFACT_GENERATION.value)
        state = orchestrator.run()
        self.assertEqual(state.current_stage, Stage.WAITING_FOR_HUMAN.value)
        decision = next(item for item in state.decisions.values() if item.status == "PENDING")
        self.assertEqual(decision.category, "ARTIFACT_PROMOTION")
        promoted = orchestrator.decide(decision.decision_id, option="promote")
        self.assertEqual(promoted.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)

    def test_external_policy_never_promotes_internally(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      promotion_policy: EXTERNAL\n")
        runner = ArtifactRunner(config.project_name)
        state = Orchestrator(config, store=StateStore(self.state_root), runner=runner).run()
        self.assertEqual(state.artifacts["spec"].status, ArtifactStatus.PROMOTION_READY.value)
        self.assertEqual(state.current_stage, Stage.WAITING_FOR_AUTHORITY_CHANGE.value)
        self.assertFalse(list((self.state_root / "artifacts" / "spec").glob("accepted")))

    def test_promotion_rejects_hash_validator_and_review_failures(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      validator_role: spec_validator\n")
        store = StateStore(self.state_root)
        candidate = store.save_artifact_candidate("spec", "candidate\n")
        artifact = EngineeringArtifact(
            "spec", "ENGINEERING_SPEC", ArtifactStatus.PROMOTION_READY.value,
            candidate_hash="0" * 64, candidate_path=str(candidate),
            validator_role="spec_validator", metadata={"review": {"verdict": "APPROVED"}, "validator": {"status": "FAIL"}},
        )
        state = WorkflowState(artifacts={"spec": artifact})
        errors = validate_artifact_promotion(config, state, "spec")
        self.assertTrue(any("hash mismatch" in error for error in errors))
        artifact.candidate_hash = hashlib.sha256(b"candidate\n").hexdigest()
        errors = validate_artifact_promotion(config, state, "spec")
        self.assertTrue(any("validator" in error for error in errors))

    def test_downstream_candidate_is_reset_when_upstream_revision_changes(self):
        config = self.config(
            "    - id: upstream\n      kind: ENGINEERING_SPEC\n      review_required: false\n"
            "    - id: downstream\n      kind: IMPLEMENTATION_DESIGN\n      dependencies: [upstream]\n      review_required: false\n"
        )
        old = "a" * 64
        new = "b" * 64
        state = WorkflowState(artifacts={
            "upstream": EngineeringArtifact("upstream", "ENGINEERING_SPEC", ArtifactStatus.ACCEPTED.value, accepted_hash=new, version_hash=new),
            "downstream": EngineeringArtifact(
                "downstream", "IMPLEMENTATION_DESIGN", ArtifactStatus.PROMOTION_READY.value,
                accepted_hash="c" * 64, candidate_hash="d" * 64,
                metadata={"dependency_revisions": [{"artifact_id": "upstream", "status": "ACCEPTED", "hash": old}]},
            ),
        })
        reconciled = reconcile_artifact_staleness(config, state)
        self.assertEqual(reconciled.artifacts["downstream"].status, ArtifactStatus.PENDING.value)
        self.assertEqual(reconciled.artifacts["downstream"].candidate_hash, None)

    def test_promotion_is_idempotent_and_persists_provenance(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      review_required: false\n")
        runner = ArtifactRunner(config.project_name)
        orchestrator = Orchestrator(config, store=StateStore(self.state_root), runner=runner)
        state = orchestrator.run()
        self.assertEqual(state.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)
        history_length = len(state.artifacts["spec"].metadata["promotion_history"])
        repeated = orchestrator._promote_artifact(state, "spec")
        self.assertEqual(repeated.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(len(repeated.artifacts["spec"].metadata["promotion_history"]), history_length)

    def test_prepared_promotion_recovers_without_accepting_half_a_revision(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n      review_required: false\n")
        store = StateStore(self.state_root)
        candidate = store.save_artifact_candidate("spec", "recoverable\n")
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        state = WorkflowState(artifacts={
            "spec": EngineeringArtifact(
                "spec", "ENGINEERING_SPEC", ArtifactStatus.PROMOTION_READY.value,
                candidate_hash=candidate_hash, candidate_path=str(candidate), review_required=False,
            ),
        })
        broken = Orchestrator(config, store=store)
        broken._atomic_write_bytes = lambda destination, content: (_ for _ in ()).throw(OSError("simulated interruption"))
        with self.assertRaises(OSError):
            broken._promote_artifact(state, "spec")
        self.assertEqual(store.load_artifact_promotion("spec")["status"], "PREPARED")
        recovered = Orchestrator(config, store=store)._promote_artifact(state, "spec")
        self.assertEqual(recovered.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(store.load_artifact_promotion("spec")["status"], "COMMITTED")

    def test_task_contract_fields_are_loaded(self):
        config = self.config("    - id: spec\n      kind: ENGINEERING_SPEC\n")
        path = Path(config.workflow_file)
        text = path.read_text().replace("      - id: coding-task", "      - id: coding-task\n        engineering_spec_anchors: [SPEC-1]\n        machine_contract_refs: [MC-1]\n        conformance_ids: [CONF-1]\n        implementation_design_refs: [DESIGN-1]")
        path.write_text(text)
        loaded = load_workflow(path, self.project)
        task = loaded.tasks[0][1]
        self.assertEqual(task.conformance_ids, ("CONF-1",))

    def test_final_verification_requires_approved_conformance_spec_and_results(self):
        state = WorkflowState(artifacts={"conf": __import__("contract_workflow.models", fromlist=["EngineeringArtifact"]).EngineeringArtifact("conf", "CONFORMANCE_SPEC", ArtifactStatus.APPROVED.value)})
        self.assertTrue(validate_final_conformance(state, {"conformance_results": []}))
        self.assertFalse(validate_final_conformance(state, {"conformance_results": [{"requirement_id": "REQ-1", "conformance_id": "CONF-1", "status": "PASS", "evidence": "test-1"}]}))

    def test_remote_accepted_human_guide_projects_without_reading_local_draft(self):
        local = self.project / "guide.md"
        local.write_text("local draft\n", encoding="utf-8")
        old = b"remote R1\n"
        old_hash = hashlib.sha256(old).hexdigest()
        workflow = self.project / ".contract-workflow" / "workflow.yaml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(f'''version: "1"\nproject:\n  name: remote-artifact-fixture\n  path: {self.project}\nmode: autonomous\nauthority:\n  remote: origin\n  branch: main\nauthoritative_sources:\n  - source_id: human-guide\n    role: HUMAN_GUIDE\n    path: guide.md\n    sha256: {old_hash}\nskills: {{}}\nrunner:\n  type: mock\ngroups:\n  - id: g\n    tasks:\n      - id: task\nartifact_pipeline:\n  artifacts:\n    - id: human-guide\n      kind: HUMAN_GUIDE\n      promotion_policy: EXTERNAL\n      review_required: false\n''', encoding="utf-8")
        config = load_workflow(workflow, self.project)
        snapshot = self.state_root / "authority" / "snapshots" / "r1" / "human-guide.md"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(old)
        store = StateStore(self.state_root)
        store.save_authority_ledger({"schema_version": "1.0", "sources": {"human-guide": {
            "source_id": "human-guide", "status": "ACCEPTED", "accepted_remote_commit": "commit-r1",
            "accepted_remote_blob": "blob-r1", "accepted_content_sha256": old_hash,
            "accepted_authority_content_sha256": old_hash, "accepted_snapshot_path": str(snapshot),
        }}})
        store.save_remote_state({"schema_version": "1.0", "sources": {"human-guide": {
            "remote_url": "https://example.invalid/pais.git", "branch": "main", "commit_sha": "commit-r1",
            "git_blob_sha": "blob-r1", "content_sha256": old_hash, "snapshot_path": str(snapshot),
        }}})
        artifact = initialize_artifacts(config, store)["human-guide"]
        self.assertEqual(artifact.status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual(artifact.accepted_hash, old_hash)
        self.assertEqual(artifact.accepted_path, str(snapshot))
        self.assertEqual(artifact.metadata["accepted_source"]["commit_sha"], "commit-r1")
        self.assertEqual(local.read_text(), "local draft\n")

    def test_remote_candidate_does_not_replace_old_accepted_projection(self):
        local = self.project / "guide.md"
        local.write_text("local draft\n", encoding="utf-8")
        old, new = b"remote R1\n", b"remote R2\n"
        old_hash, new_hash = hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest()
        workflow = self.project / ".contract-workflow" / "workflow.yaml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(f'''version: "1"\nproject:\n  name: remote-candidate-fixture\n  path: {self.project}\nmode: autonomous\nauthority:\n  remote: origin\n  branch: main\nauthoritative_sources:\n  - source_id: human-guide\n    role: HUMAN_GUIDE\n    path: guide.md\n    sha256: {old_hash}\nskills: {{}}\nrunner:\n  type: mock\ngroups:\n  - id: g\n    tasks:\n      - id: task\nartifact_pipeline:\n  artifacts:\n    - id: human-guide\n      kind: HUMAN_GUIDE\n      promotion_policy: EXTERNAL\n      review_required: false\n''', encoding="utf-8")
        config = load_workflow(workflow, self.project)
        old_snapshot = self.state_root / "authority" / "snapshots" / "r1" / "human-guide.md"
        new_snapshot = self.state_root / "authority" / "snapshots" / "r2" / "human-guide.md"
        old_snapshot.parent.mkdir(parents=True, exist_ok=True)
        new_snapshot.parent.mkdir(parents=True, exist_ok=True)
        old_snapshot.write_bytes(old)
        new_snapshot.write_bytes(new)
        store = StateStore(self.state_root)
        store.save_authority_ledger({"schema_version": "1.0", "sources": {"human-guide": {
            "source_id": "human-guide", "status": "CHANGE_PENDING", "accepted_remote_commit": "commit-r1",
            "accepted_remote_blob": "blob-r1", "accepted_content_sha256": old_hash,
            "accepted_authority_content_sha256": old_hash, "accepted_snapshot_path": str(old_snapshot),
            "candidate_remote_commit": "commit-r2", "candidate_remote_blob": "blob-r2",
            "candidate_content_sha256": new_hash,
        }}})
        store.save_remote_state({"schema_version": "1.0", "sources": {"human-guide": {
            "remote_url": "https://example.invalid/pais.git", "branch": "main", "commit_sha": "commit-r2",
            "git_blob_sha": "blob-r2", "content_sha256": new_hash, "snapshot_path": str(new_snapshot),
        }}})
        artifact = initialize_artifacts(config, store)["human-guide"]
        self.assertEqual(artifact.status, ArtifactStatus.PROMOTION_READY.value)
        self.assertEqual(artifact.accepted_hash, old_hash)
        self.assertEqual(artifact.candidate_hash, new_hash)
        self.assertIsNone(artifact.accepted_path)
        self.assertEqual(artifact.candidate_path, str(new_snapshot))
        self.assertEqual(artifact.metadata["candidate_source"]["commit_sha"], "commit-r2")


if __name__ == "__main__":
    unittest.main()
