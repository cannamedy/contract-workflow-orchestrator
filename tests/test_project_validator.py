from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from contract_workflow.config import WorkflowConfigError, load_workflow
from contract_workflow.models import ArtifactSpec, ArtifactStatus, EngineeringArtifact, Stage, WorkflowState
from contract_workflow.orchestrator import Orchestrator
from contract_workflow.project_validator import execute_project_validator
from contract_workflow.state_store import StateStore


VALIDATOR = textwrap.dedent(
    """
    import hashlib, json, pathlib, sys
    artifact = pathlib.Path('artifact.md')
    mode = pathlib.Path('validator-mode').read_text().strip()
    status = {'warn': 'PASS_WITH_WARNINGS', 'fail': 'FAIL', 'missing': 'ARTIFACT_MISSING'}.get(mode, 'PASS')
    result = {
        'validator': sys.argv[1], 'status': status,
        'artifact': 'artifact.md',
        'source_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.is_file() else None,
        'findings': [], 'coverage': {}
    }
    if mode == 'mutate':
        pathlib.Path('validator-mutated.txt').write_text('bad')
    print(json.dumps(result))
    raise SystemExit(1 if status in {'FAIL', 'ARTIFACT_MISSING'} else 0)
    """
).strip() + "\n"


class ValidatorSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "validator.py").write_text(VALIDATOR, encoding="utf-8")
        (self.project / "validator-mode").write_text("pass\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.state_root = self.root / "state"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_config(self, *, validator_role: str | None = "validator"):
        role_line = f"      validator_role: {validator_role}\n" if validator_role else ""
        workflow = self.project / "workflow.yaml"
        workflow.write_text(
            textwrap.dedent(
                f"""
                version: "1"
                project:
                  name: validator-fixture
                  path: {self.project}
                mode: autonomous
                authoritative_sources: []
                skills: {{}}
                runner:
                  type: mock
                project_validators:
                  entrypoint: validator.py
                  invocation: "python3 {{entrypoint}} {{validator_role}} --project {{project}} --json"
                  roles:
                    validator: validator
                groups:
                  - id: g
                    tasks:
                      - id: task
                artifact_pipeline:
                  artifacts:
                    - id: spec
                      kind: ENGINEERING_SPEC
                      accepted_path: artifact.md
                      review_required: true
                {role_line}
                """
            ),
            encoding="utf-8",
        )
        config = load_workflow(workflow, self.project)
        spec = ArtifactSpec("spec", "ENGINEERING_SPEC", validator_role=validator_role, accepted_path="artifact.md")
        return config, spec

    def artifact_and_candidate(self, config, spec, content: bytes = b"candidate\n"):
        store = StateStore(self.state_root)
        candidate = store.save_artifact_candidate("spec", content.decode())
        artifact = EngineeringArtifact(
            id="spec", kind="ENGINEERING_SPEC", status=ArtifactStatus.CANDIDATE.value,
            candidate_hash=hashlib.sha256(content).hexdigest(), candidate_path=str(candidate),
            validator_role=spec.validator_role, accepted_path=spec.accepted_path,
        )
        return store, artifact

    def run_validator(self, mode: str = "pass"):
        (self.project / "validator-mode").write_text(mode + "\n", encoding="utf-8")
        config, spec = self.make_config()
        store, artifact = self.artifact_and_candidate(config, spec)
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": artifact})
        result = execute_project_validator(config, state, artifact, spec, state_root=self.state_root, upstream_hashes=[], timeout_seconds=10)
        return result, store

    def test_config_rejects_unknown_placeholder_and_accepts_canonical_template(self):
        config, _ = self.make_config()
        self.assertEqual(config.project_validators.roles["validator"], "validator")
        workflow = Path(config.workflow_file)
        workflow.write_text(workflow.read_text().replace("{project}", "{unknown}"), encoding="utf-8")
        with self.assertRaises(WorkflowConfigError):
            load_workflow(workflow, self.project)

    def test_pass_uses_exact_candidate_and_preserves_real_project(self):
        result, _ = self.run_validator()
        self.assertEqual(result.kind, "PASS")
        self.assertEqual(result.evidence["status"], "PASS")
        self.assertEqual((self.project / "artifact.md").exists(), False)
        self.assertEqual(result.evidence["source_sha256"], result.evidence["candidate_hash"])

    def test_pass_with_warnings_is_accepted_for_review(self):
        result, _ = self.run_validator("warn")
        self.assertEqual(result.kind, "PASS")
        self.assertEqual(result.evidence["status"], "PASS_WITH_WARNINGS")

    def test_fail_and_artifact_missing_are_artifact_failures(self):
        failed, _ = self.run_validator("fail")
        missing, _ = self.run_validator("missing")
        self.assertEqual(failed.kind, "ARTIFACT_FAIL")
        self.assertEqual(failed.code, "FAIL")
        self.assertEqual(missing.kind, "ARTIFACT_FAIL")
        self.assertEqual(missing.code, "ARTIFACT_MISSING")

    def test_malformed_json_is_infrastructure_failure(self):
        (self.project / "validator.py").write_text("print('PASS')\n", encoding="utf-8")
        result, _ = self.run_validator()
        self.assertEqual(result.kind, "INFRA_FAIL")
        self.assertEqual(result.code, "PROJECT_VALIDATOR_EXECUTION_FAILED")

    def test_nonzero_pass_is_infrastructure_failure(self):
        (self.project / "validator.py").write_text("import json,sys\nprint(json.dumps({'validator': sys.argv[1], 'status':'PASS', 'source_sha256': '" + "0" * 64 + "'}))\nsys.exit(1)\n", encoding="utf-8")
        result, _ = self.run_validator()
        self.assertEqual(result.kind, "INFRA_FAIL")
        self.assertIn(result.code, {"VALIDATOR_CANDIDATE_HASH_MISMATCH", "PROJECT_VALIDATOR_EXECUTION_FAILED"})

    def test_candidate_hash_mismatch_is_rejected(self):
        config, spec = self.make_config()
        store, artifact = self.artifact_and_candidate(config, spec)
        artifact.candidate_hash = "0" * 64
        result = execute_project_validator(config, WorkflowState(project_path=str(self.project)), artifact, spec, state_root=self.state_root, upstream_hashes=[], timeout_seconds=10)
        self.assertEqual(result.code, "VALIDATOR_CANDIDATE_HASH_MISMATCH")

    def test_validator_mutation_is_detected_and_real_tree_stays_unchanged(self):
        result, _ = self.run_validator("mutate")
        self.assertEqual(result.code, "VALIDATOR_MUTATED_WORKSPACE")
        self.assertFalse((self.project / "validator-mutated.txt").exists())

    def test_missing_candidate_is_not_run_as_a_validator(self):
        config, spec = self.make_config()
        artifact = EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.CANDIDATE.value, candidate_hash="0" * 64, candidate_path=str(self.state_root / "missing"), validator_role="validator")
        result = execute_project_validator(config, WorkflowState(project_path=str(self.project)), artifact, spec, state_root=self.state_root, upstream_hashes=[], timeout_seconds=10)
        self.assertEqual(result.code, "PROJECT_VALIDATOR_EXECUTION_FAILED")

    def test_validator_entrypoint_failure_is_infrastructure_failure(self):
        config, spec = self.make_config()
        config = config.__class__(**{**config.__dict__, "project_validators": config.project_validators.__class__("missing.py", config.project_validators.invocation, config.project_validators.roles)})
        store, artifact = self.artifact_and_candidate(config, spec)
        result = execute_project_validator(config, WorkflowState(project_path=str(self.project)), artifact, spec, state_root=self.state_root, upstream_hashes=[], timeout_seconds=10)
        self.assertEqual(result.kind, "INFRA_FAIL")

    def test_orchestrator_routes_generation_through_validation_before_review(self):
        config, _ = self.make_config()
        config = config.__class__(**{**config.__dict__, "artifact_pipeline": (ArtifactSpec("spec", "ENGINEERING_SPEC", validator_role="validator", accepted_path="artifact.md"),)})
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        content = "candidate\n"
        outcome = {"verdict": "APPROVED", "summary": "generated", "artifact": {"id": "spec", "kind": "ENGINEERING_SPEC", "candidate_content": content, "candidate_hash": hashlib.sha256(content.encode()).hexdigest()}}
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.GENERATING.value, validator_role="validator", accepted_path="artifact.md")}, current_artifact_id="spec", current_stage=Stage.ARTIFACT_GENERATION.value)
        applied = orchestrator._apply_artifact_outcome(state, outcome).state
        self.assertEqual(applied.current_stage, Stage.ARTIFACT_VALIDATION.value)
        validated = orchestrator._artifact_validation_step(applied).state
        self.assertEqual(validated.current_stage, Stage.ARTIFACT_REVIEW.value)
        self.assertEqual(validated.artifacts["spec"].metadata["validator"]["status"], "PASS")

    def test_validator_failure_routes_to_patch_without_human_decision(self):
        (self.project / "validator-mode").write_text("fail\n", encoding="utf-8")
        config, _ = self.make_config()
        config = config.__class__(**{**config.__dict__, "artifact_pipeline": (ArtifactSpec("spec", "ENGINEERING_SPEC", validator_role="validator", accepted_path="artifact.md"),)})
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        content = "candidate\n"
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.CANDIDATE.value, candidate_hash=hashlib.sha256(content.encode()).hexdigest(), candidate_path=str(store.save_artifact_candidate("spec", content)), validator_role="validator", accepted_path="artifact.md")}, current_artifact_id="spec", current_stage=Stage.ARTIFACT_VALIDATION.value)
        result = orchestrator._artifact_validation_step(state).state
        self.assertEqual(result.current_stage, Stage.ARTIFACT_PATCH.value)
        self.assertEqual(result.artifacts["spec"].status, ArtifactStatus.REQUIRES_PATCH.value)
        self.assertFalse(result.decisions)

    def test_candidate_replacement_invalidates_previous_validator_and_review_evidence(self):
        config, _ = self.make_config()
        store = StateStore(self.state_root)
        old = b"old\n"
        old_path = store.save_artifact_candidate("spec", old.decode())
        artifact = EngineeringArtifact(
            "spec", "ENGINEERING_SPEC", ArtifactStatus.REQUIRES_PATCH.value,
            candidate_hash=hashlib.sha256(old).hexdigest(), candidate_path=str(old_path),
            validator_role="validator", accepted_path="artifact.md",
            metadata={"validator": {"status": "PASS", "source_sha256": hashlib.sha256(old).hexdigest()}, "review": {"verdict": "APPROVED"}},
        )
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": artifact}, current_artifact_id="spec", current_stage=Stage.ARTIFACT_PATCH.value)
        new = b"new\n"
        outcome = {"verdict": "APPROVED", "artifact": {"id": "spec", "kind": "ENGINEERING_SPEC", "candidate_content": new.decode(), "candidate_hash": hashlib.sha256(new).hexdigest()}}
        updated = Orchestrator(config, store=store)._apply_artifact_outcome(state, outcome).state
        self.assertEqual(updated.current_stage, Stage.ARTIFACT_VALIDATION.value)
        self.assertNotIn("validator", updated.artifacts["spec"].metadata)
        self.assertNotIn("review", updated.artifacts["spec"].metadata)

    def test_patch_candidate_is_revalidated_before_review(self):
        config, _ = self.make_config()
        (self.project / "validator-mode").write_text("fail\n", encoding="utf-8")
        store = StateStore(self.state_root)
        first = b"first\n"
        artifact = EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.CANDIDATE.value, candidate_hash=hashlib.sha256(first).hexdigest(), candidate_path=str(store.save_artifact_candidate("spec", first.decode())), validator_role="validator", accepted_path="artifact.md")
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": artifact}, current_artifact_id="spec", current_stage=Stage.ARTIFACT_VALIDATION.value)
        orchestrator = Orchestrator(config, store=store)
        failed = orchestrator._artifact_validation_step(state).state
        self.assertEqual(failed.current_stage, Stage.ARTIFACT_PATCH.value)
        (self.project / "validator-mode").write_text("pass\n", encoding="utf-8")
        second = b"second\n"
        patched = orchestrator._apply_artifact_outcome(failed, {"verdict": "APPROVED", "artifact": {"id": "spec", "kind": "ENGINEERING_SPEC", "candidate_content": second.decode(), "candidate_hash": hashlib.sha256(second).hexdigest()}}).state
        self.assertEqual(patched.current_stage, Stage.ARTIFACT_VALIDATION.value)
        reviewed = orchestrator._artifact_validation_step(patched).state
        self.assertEqual(reviewed.current_stage, Stage.ARTIFACT_REVIEW.value)

    def test_internal_plan_graph_role_is_not_sent_to_project_subprocess(self):
        from contract_workflow.project_validator import requires_project_validation
        self.assertFalse(requires_project_validation("plan_graph_validator"))
        self.assertTrue(requires_project_validation("machine_contract_project_validator"))

    def test_validator_evidence_contains_provenance_and_external_logs(self):
        result, _ = self.run_validator()
        evidence = result.evidence
        self.assertEqual(evidence["artifact_kind"], "ENGINEERING_SPEC")
        self.assertTrue(Path(evidence["stdout_path"]).is_file())
        self.assertTrue(Path(evidence["stderr_path"]).is_file())
        self.assertIn("upstream_hashes", evidence)

    def test_pass_with_warnings_can_reach_deterministic_promotion(self):
        (self.project / "validator-mode").write_text("warn\n", encoding="utf-8")
        config, _ = self.make_config()
        config = config.__class__(**{**config.__dict__, "artifact_pipeline": (ArtifactSpec("spec", "ENGINEERING_SPEC", validator_role="validator", accepted_path="artifact.md", review_required=False),)})
        store = StateStore(self.state_root)
        content = b"candidate\n"
        artifact = EngineeringArtifact("spec", "ENGINEERING_SPEC", ArtifactStatus.CANDIDATE.value, candidate_hash=hashlib.sha256(content).hexdigest(), candidate_path=str(store.save_artifact_candidate("spec", content.decode())), validator_role="validator", accepted_path="artifact.md", review_required=False)
        state = WorkflowState(project_path=str(self.project), artifacts={"spec": artifact}, current_artifact_id="spec", current_stage=Stage.ARTIFACT_VALIDATION.value)
        promoted = Orchestrator(config, store=store)._artifact_validation_step(state).state
        self.assertEqual(promoted.artifacts["spec"].status, ArtifactStatus.ACCEPTED.value)
        self.assertEqual((self.project / "artifact.md").read_bytes(), content)

    def test_config_without_validator_role_does_not_require_external_validation(self):
        config, spec = self.make_config(validator_role=None)
        self.assertIsNone(spec.validator_role)
        self.assertIsNotNone(config.project_validators)


if __name__ == "__main__":
    unittest.main()
