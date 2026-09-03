from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.artifacts import artifact_impact_closure, missing_skill_roles, validate_artifact_graph, validate_final_conformance
from contract_workflow.config import WorkflowConfigError, load_workflow
from contract_workflow.models import ArtifactSpec, ArtifactStatus, Stage, WorkflowState
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
        self.assertEqual(approved.artifacts["spec"].status, ArtifactStatus.APPROVED.value)
        self.assertTrue(list((self.state_root / "artifacts" / "spec").glob("candidate")))

    def test_optional_artifact_can_be_skipped_and_missing_skill_is_diagnostic(self):
        config = self.config("    - id: optional\n      kind: MACHINE_CONTRACT\n      optional: true\n      enabled: false\n      skill_role: machine_contract\n")
        self.assertEqual(missing_skill_roles(config), ["machine_contract"])

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


if __name__ == "__main__":
    unittest.main()
