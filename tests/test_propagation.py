from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import Stage, Verdict, WorkItemStatus, WorkflowState
from contract_workflow.orchestrator import Orchestrator, OrchestratorError
from contract_workflow.outcome import make_outcome
from contract_workflow.plan_graph import reconcile_plan_graph, validate_plan_graph
from contract_workflow.runners.base import RunnerResult, run_times
from contract_workflow.scheduler import recompute, ready_work
from contract_workflow.state_store import StateStore


class PropagationRunner:
    """Structured stage fixture; it never writes project authority files."""

    def __init__(self, store: StateStore, *, contract: str, plan: str, graph: dict, review_patch: bool = False):
        self.store = store
        self.contract = contract
        self.plan = plan
        self.graph = graph
        self.review_patch = review_patch
        self.contract_reviews = 0

    def run(self, cwd, prompt, run_dir, timeout, env=None):
        state = self.store.load()
        stage = state.current_stage
        extras = {}
        verdict = Verdict.APPROVED.value
        if stage == Stage.AUTHORITY_CHANGE_ANALYSIS.value:
            change = state.authority_changes[state.current_authority_change_id]
            extras["authority_change"] = {
                "change_id": change["change_id"],
                "base_sha256": change["base_sha256"],
                "candidate_sha256": change["candidate_sha256"],
                "classification": "C2",
                "semantic_change": True,
                "affected_requirements": ["REQ-A"],
                "affected_contract_anchors": [],
                "directly_affected_tasks": ["A"],
                "dependency_affected_tasks": [],
                "unaffected_tasks": [],
                "machine_resolvable": True,
                "human_decision_required": False,
                "human_decision_requests": [],
                "required_propagation": [
                    "CONTRACT_REVISION_REQUIRED", "PLAN_REVISION_REQUIRED", "TASK_REBASE_REQUIRED",
                ],
                "analysis_summary": "The candidate changes an explicit normative behavior.",
            }
        elif stage == Stage.CHANGE_PROPAGATION_PLANNING.value:
            extras["propagation_plan"] = {"stages": state.propagation[state.current_authority_change_id]["stages"]}
        elif stage == Stage.CONTRACT_REVISION.value:
            extras["candidate_artifacts"] = [{"path": "contract.md", "content": self.contract}]
            extras["contract_revision_report"] = {"changed_anchors": ["§3"]}
        elif stage == Stage.CONTRACT_REVISION_REVIEW.value:
            self.contract_reviews += 1
            if self.review_patch and self.contract_reviews == 1:
                verdict = Verdict.REQUIRES_PATCH.value
            extras["contract_review"] = {"verdict": verdict, "summary": "independent contract review"}
        elif stage == Stage.PLAN_REVISION.value:
            extras["candidate_artifacts"] = [{"path": "plan.md", "content": self.plan}]
            extras["plan_revision_report"] = {"changed_tasks": ["A"]}
        elif stage == Stage.PLAN_REVISION_REVIEW.value:
            extras["plan_review"] = {"verdict": verdict, "summary": "independent plan review"}
        elif stage == Stage.PLAN_GRAPH_BUILD.value:
            extras["plan_graph"] = self.graph
        elif stage == Stage.TASK_REBASE_ANALYSIS.value:
            extras["task_rebase"] = {"tasks": [{"task_id": "A", "action": "rebase existing implementation", "preserved_review_findings": ["capabilities / frames scalar must not be converted to tuple"]}]}
        outcome = make_outcome(env["CWO_RUN_ID"], stage, state.project, verdict, **extras)
        (run_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        started, finished = run_times()
        return RunnerResult(0, run_dir / "stdout.log", run_dir / "stderr.log", started, finished)


class PropagationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "-C", str(self.project), "init", "-q"], check=True)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)
        (self.project / "guide.md").write_text("accepted guide\n", encoding="utf-8")
        (self.project / "contract.md").write_text("accepted contract §3\n", encoding="utf-8")
        (self.project / "plan.md").write_text("accepted plan\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", "fixture"], check=True)

    def tearDown(self):
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    @staticmethod
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def config(self):
        control = self.project / ".contract-workflow"
        control.mkdir(exist_ok=True)
        lines = [
            'version: "1"', 'project:', '  name: propagation-fixture', f'  path: {self.project}',
            'mode: autonomous', 'authoritative_sources:',
            '  - source_id: human-guide', '    role: HUMAN_GUIDE', '    path: guide.md', f'    sha256: {self.sha(self.project / "guide.md")}',
            '  - source_id: engineering-contract', '    role: ENGINEERING_CONTRACT', '    path: contract.md', f'    sha256: {self.sha(self.project / "contract.md")}',
            '  - source_id: implementation-plan', '    role: IMPLEMENTATION_PLAN', '    path: plan.md', f'    sha256: {self.sha(self.project / "plan.md")}',
            'skills: {}', 'runner:', '  type: mock', 'policy:', '  max_attempts_per_stage: 2', '  max_total_steps: 80',
            '  retry_backoff_seconds: 0', 'groups:', '  - id: g', '    tasks:', '      - id: A', '        requirement_ids: [REQ-A]',
            '        contract_anchors: [§3]', '',
        ]
        (control / "workflow.yaml").write_text("\n".join(lines), encoding="utf-8")
        return load_workflow(control / "workflow.yaml", self.project)

    def graph(self, plan: str) -> dict:
        return {
            "plan_sha256": hashlib.sha256(plan.encode()).hexdigest(),
            "tasks": [
                {"id": "A", "group": "g", "dependencies": [], "requirement_ids": ["REQ-A"], "contract_anchors": ["§3"], "allowed_paths": ["src/a.py"], "expected_outputs": ["tests/a.py"]},
                {"id": "B", "group": "g", "dependencies": ["A"], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/b.py"], "expected_outputs": ["tests/b.py"]},
                {"id": "C", "group": "g", "dependencies": ["A"], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/c.py"], "expected_outputs": ["tests/c.py"]},
            ],
        }

    def start_change(self):
        config = self.config()
        store = StateStore(self.state_root)
        orchestrator = Orchestrator(config, store=store)
        orchestrator.step()  # initial scheduler projection: the configured A
        (self.project / "guide.md").write_text("candidate guide\n", encoding="utf-8")
        detected = orchestrator.step()
        self.assertEqual(detected.action, "authority_change_analysis")
        return config, store

    def test_machine_propagation_builds_full_graph_and_waits_only_at_promotion(self):
        config, store = self.start_change()
        plan = "candidate plan with structured graph\n"
        graph = self.graph(plan)
        runner = PropagationRunner(store, contract="candidate contract §3\n", plan=plan, graph=graph)
        final = Orchestrator(config, store=store, runner=runner).run()
        self.assertEqual(final.current_stage, Stage.WAITING_FOR_HUMAN.value)
        self.assertEqual(len(final.plan_graph["tasks"]), 3)
        self.assertEqual(final.plan_graph_reconciliation["new"], ["A", "B", "C"])
        self.assertEqual(final.work_items["A"].status, WorkItemStatus.TASK_REBASE_REQUIRED.value)
        self.assertEqual(final.work_items["B"].status, WorkItemStatus.WAITING_DEPENDENCY.value)
        self.assertEqual(final.work_items["C"].status, WorkItemStatus.WAITING_DEPENDENCY.value)
        change_id = final.current_authority_change_id
        self.assertIn("capabilities / frames scalar", final.propagation[change_id]["task_rebase"]["tasks"][0]["preserved_review_findings"][0])
        decision = next(item for item in final.decisions.values() if item.status == "PENDING")
        self.assertEqual(decision.category, "AUTHORITY_PROMOTION")
        ledger = json.loads((store.authority_path / "ledger.json").read_text())
        self.assertEqual(ledger["sources"]["human-guide"]["status"], "PROPAGATING")
        self.assertFalse((self.project / "contract.md").read_text() == "candidate contract §3\n")
        self.assertTrue((store.propagation_path / final.current_authority_change_id / "candidates" / "contract.md").is_file())

    def test_contract_review_requires_patch_then_rereviews_without_human(self):
        config, store = self.start_change()
        plan = "candidate plan\n"
        runner = PropagationRunner(store, contract="candidate contract §3\n", plan=plan, graph=self.graph(plan), review_patch=True)
        final = Orchestrator(config, store=store, runner=runner).run()
        self.assertEqual(final.current_stage, Stage.WAITING_FOR_HUMAN.value)
        self.assertEqual(runner.contract_reviews, 2)
        self.assertNotEqual(final.stop_code, "RETRY_EXHAUSTED")

    def test_promotion_is_hash_checked_and_idempotent_preflight(self):
        config, store = self.start_change()
        plan = "candidate plan\n"
        runner = PropagationRunner(store, contract="candidate contract §3\n", plan=plan, graph=self.graph(plan))
        state = Orchestrator(config, store=store, runner=runner).run()
        decision = next(item for item in state.decisions.values() if item.status == "PENDING")
        (self.project / "guide.md").write_text("candidate changed again\n", encoding="utf-8")
        with self.assertRaises(OrchestratorError):
            Orchestrator(config, store=store).decide(decision.decision_id, option="promote")
        self.assertEqual(Orchestrator(config, store=store).show_decision(decision.decision_id).status, "PENDING")

    def test_promotion_atomically_accepts_candidate_authorities_and_preserves_digest(self):
        config, store = self.start_change()
        original_workflow_digest = config.digest
        plan = "candidate plan\n"
        runner = PropagationRunner(store, contract="candidate contract §3\n", plan=plan, graph=self.graph(plan))
        state = Orchestrator(config, store=store, runner=runner).run()
        decision = next(item for item in state.decisions.values() if item.status == "PENDING")
        promoted = Orchestrator(config, store=store).decide(decision.decision_id, option="promote")
        self.assertEqual(promoted.authority_changes[decision.source_change]["status"], "PROMOTED")
        self.assertEqual(promoted.propagation[decision.source_change]["status"], "PROMOTED")
        self.assertEqual(promoted.current_stage, Stage.TASK_PATCH.value)
        self.assertEqual(config.digest, original_workflow_digest)
        self.assertEqual((self.project / "contract.md").read_text(), "candidate contract §3\n")
        self.assertEqual((self.project / "plan.md").read_text(), plan)
        ledger = json.loads((store.authority_path / "ledger.json").read_text())["sources"]
        self.assertEqual(ledger["human-guide"]["status"], "ACCEPTED")
        self.assertEqual(ledger["engineering-contract"]["status"], "ACCEPTED")
        self.assertEqual(ledger["implementation-plan"]["status"], "ACCEPTED")

    def test_unaffected_ready_branch_is_selected_from_projected_graph(self):
        config, store = self.start_change()
        state = store.load()
        plan = "candidate plan\n"
        graph = self.graph(plan)
        graph["tasks"].append({"id": "D", "group": "g", "dependencies": [], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/d.py"], "expected_outputs": ["tests/d.py"]})
        change_id = state.current_authority_change_id
        state = WorkflowState.from_dict({**state.to_dict(), "plan_graph": graph, "authority_changes": {change_id: {**state.authority_changes[change_id], "status": "PROPAGATING", "directly_affected_tasks": ["A"], "dependency_affected_tasks": ["B", "C"]}}, "propagation": {change_id: {"status": "RUNNING", "next_stage": Stage.CHANGE_PROPAGATION_PLANNING.value}}})
        state = recompute(config, state)
        self.assertEqual([item.id for item in ready_work(config, state)], ["D"])

    def test_graph_cycle_unknown_dependency_and_digest_are_rejected(self):
        config = self.config()
        cycle = {"plan_sha256": "a" * 64, "tasks": [{"id": "A", "group": "g", "dependencies": ["B"]}, {"id": "B", "group": "g", "dependencies": ["A"]}]}
        self.assertTrue(any("cycle" in error for error in validate_plan_graph(config, cycle)[1]))
        unknown = {"plan_sha256": "a" * 64, "tasks": [{"id": "A", "group": "g", "dependencies": ["MISSING"]}]}
        self.assertTrue(any("unknown dependency" in error for error in validate_plan_graph(config, unknown)[1]))
        valid = {"plan_sha256": "a" * 64, "tasks": [{"id": "A", "group": "g", "dependencies": []}], "graph_sha256": "b" * 64}
        self.assertIn("graph digest", " ".join(validate_plan_graph(config, valid)[1]))

    def test_graph_reconciliation_preserves_completed_and_records_supersession(self):
        config = self.config()
        state = __import__("contract_workflow.models", fromlist=["WorkflowState"]).WorkflowState(project="p", plan_graph={"graph_sha256": "old", "tasks": [
            {"id": "A", "group": "g", "dependencies": [], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/a.py"], "expected_outputs": ["tests/a.py"], "skill_role": None},
            {"id": "B", "group": "g", "dependencies": [], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/b.py"], "expected_outputs": ["tests/b.py"], "skill_role": None},
        ]})
        from contract_workflow.models import WorkItemState
        state.work_items = {"A": WorkItemState("A", "g", WorkItemStatus.COMPLETED.value), "B": WorkItemState("B", "g", WorkItemStatus.READY.value)}
        graph = {"plan_sha256": "a" * 64, "tasks": [
            {"id": "A", "group": "g", "dependencies": [], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/a.py"], "expected_outputs": ["tests/a.py"]},
            {"id": "C", "group": "g", "dependencies": [], "requirement_ids": [], "contract_anchors": [], "allowed_paths": ["src/c.py"], "expected_outputs": ["tests/c.py"]},
        ]}
        normalized, errors = validate_plan_graph(config, graph)
        self.assertFalse(errors)
        result = reconcile_plan_graph(config, state, normalized, {"B"})
        self.assertEqual(result["completed_unaffected"], ["A"])
        self.assertEqual(result["superseded"], ["B"])
        self.assertEqual(result["new"], ["C"])


if __name__ == "__main__":
    unittest.main()
