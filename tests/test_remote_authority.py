from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import HumanDecision, WorkflowState
from contract_workflow.remote import (
    NEWER_REMOTE_REVISION_AVAILABLE,
    REMOTE_AUTHORITY_SOURCE_MISSING,
    REMOTE_CHECK_FAILED,
    check_remote_authority,
)
from contract_workflow.state_store import StateStore


class RemoteAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.remote = self.root / "remote.git"
        self.project.mkdir()
        subprocess.run(["git", "-C", str(self.project), "init", "-q", "-b", "main"], check=True)
        self.guide = self.project / "guide.md"
        self.guide.write_text("R1\n", encoding="utf-8")
        (self.project / "unrelated.txt").write_text("one\n", encoding="utf-8")
        self._commit("R1")
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.project), "push", "-q", "-u", "origin", "main"], check=True)
        self.state_root = self.root / "state"
        self.workflow = self.project / ".contract-workflow" / "workflow.yaml"
        self.workflow.parent.mkdir()
        self.old_sha = hashlib.sha256(b"R1\n").hexdigest()
        self.workflow.write_text("""version: \"1\"\nproject:\n  name: remote-fixture\n  path: %s\nmode: autonomous\nauthority:\n  remote: origin\n  branch: main\nauthoritative_sources:\n  - source_id: human-guide\n    role: HUMAN_GUIDE\n    path: guide.md\n    sha256: %s\nskills: {}\nrunner:\n  type: mock\ngroups:\n  - id: g\n    tasks:\n      - id: task\n""" % (self.project, self.old_sha), encoding="utf-8")
        self.config = load_workflow(self.workflow, self.project)
        self.store = StateStore(self.state_root)
        self.state = WorkflowState(project=self.config.project_name, project_path=self.config.project_path, workflow_file=self.config.workflow_file, workflow_digest=self.config.digest)

    def tearDown(self):
        self.temp.cleanup()

    def _commit(self, message: str):
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", message], check=True)

    def _remote_commit(self, guide: str | None = None, unrelated: str | None = None, message: str = "next"):
        if guide is not None:
            self.guide.write_text(guide, encoding="utf-8")
        if unrelated is not None:
            (self.project / "unrelated.txt").write_text(unrelated, encoding="utf-8")
        self._commit(message)
        subprocess.run(["git", "-C", str(self.project), "push", "-q", "origin", "main"], check=True)

    def test_initial_and_unchanged_remote_are_no_change(self):
        first = check_remote_authority(self.config, self.store, self.state)
        second = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(first.status, "NO_CHANGE")
        self.assertEqual(second.status, "NO_CHANGE")
        self.assertEqual(len(list(self.store.authority_changes_path.glob("CR-*.json"))), 0)
        ledger = json.loads(self.store.authority_ledger_path.read_text())
        self.assertTrue(ledger["sources"]["human-guide"]["accepted_remote_blob"])
        self.assertTrue(Path(ledger["sources"]["human-guide"]["accepted_snapshot_path"]).is_file())

    def test_unrelated_remote_commit_does_not_trigger(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(unrelated="two\n", message="unrelated")
        result = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(result.status, "NO_CHANGE")
        self.assertIsNone(result.new_change)

    def test_remote_guide_change_creates_snapshot_and_change_record(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(guide="R2\n", message="guide R2")
        result = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(result.status, "AUTHORITY_CHANGE_DETECTED")
        self.assertIsNotNone(result.new_change)
        change = result.new_change
        self.assertEqual(change["authority_origin"], "git-remote")
        self.assertTrue(Path(change["candidate_snapshot_path"]).is_file())
        self.assertEqual(hashlib.sha256(Path(change["candidate_snapshot_path"]).read_bytes()).hexdigest(), change["candidate_sha256"])
        self.assertEqual(change["candidate_commit"], result.snapshot.commit_sha)
        self.assertEqual(change["candidate_blob_sha"], result.snapshot.git_blob_sha)

    def test_same_blob_is_idempotent_and_newer_revision_is_queued(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(guide="R2\n", message="guide R2")
        first = check_remote_authority(self.config, self.store, self.state)
        second = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(second.status, "CHANGE_PENDING")
        self.assertEqual(first.new_change["change_id"], json.loads(self.store.authority_ledger_path.read_text())["sources"]["human-guide"]["change_id"])
        self._remote_commit(guide="R3\n", message="guide R3")
        newer = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(newer.status, NEWER_REMOTE_REVISION_AVAILABLE)
        self.assertEqual(len(list(self.store.authority_changes_path.glob("CR-*.json"))), 1)

    def test_unaccepted_candidate_rolls_over_in_same_change_and_preserves_history(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(guide="R2\n", message="guide R2")
        first = check_remote_authority(self.config, self.store, self.state)
        change_id = first.new_change["change_id"]
        old_hash = first.new_change["candidate_sha256"]
        old_decision = HumanDecision(
            decision_id=f"ADR-HUMAN-GUIDE-PROMOTION-{change_id}",
            category="AUTHORITY_PROMOTION", question=f"Approve revision {old_hash}?",
            source_change=change_id, source_artifact_id="human-guide", source_candidate_hash=old_hash,
        )
        self.state.decisions[old_decision.decision_id] = old_decision
        self.store.save_decision(old_decision)

        self._remote_commit(guide="R3\n", message="guide R3")
        rolled = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(rolled.status, NEWER_REMOTE_REVISION_AVAILABLE)
        self.assertEqual(rolled.rollover["change_id"], change_id)
        change = json.loads((self.store.authority_changes_path / f"{change_id}.json").read_text())
        revisions = {item["content_sha256"]: item for item in change["candidate_revisions"]}
        new_hash = rolled.snapshot.content_sha256
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[old_hash]["status"], "SUPERSEDED")
        self.assertEqual(revisions[old_hash]["superseded_reason"], "NEWER_HUMAN_AUTHORITY_SUBMISSION")
        self.assertEqual(revisions[new_hash]["status"], "ACTIVE")
        self.assertEqual(change["candidate_sha256"], new_hash)
        self.assertEqual(change["base_sha256"], self.old_sha)
        superseded = HumanDecision.from_dict(json.loads((self.store.decisions_path / f"{old_decision.decision_id}.json").read_text()))
        self.assertEqual(superseded.status, "SUPERSEDED")
        self.assertIn(new_hash[:12].upper(), superseded.superseded_by)
        self.assertEqual(superseded.question, old_decision.question)

        repeated = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(repeated.status, "CHANGE_PENDING")
        self.assertIsNone(repeated.rollover)
        change_again = json.loads((self.store.authority_changes_path / f"{change_id}.json").read_text())
        self.assertEqual(len(change_again["candidate_revisions"]), 2)

    def test_accepted_candidate_starts_new_change_instead_of_rollover(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(guide="R2\n", message="guide R2")
        first = check_remote_authority(self.config, self.store, self.state)
        ledger = json.loads(self.store.authority_ledger_path.read_text())
        entry = ledger["sources"]["human-guide"]
        entry.update({
            "accepted_remote_commit": entry["candidate_remote_commit"],
            "accepted_remote_blob": entry["candidate_remote_blob"],
            "accepted_content_sha256": entry["candidate_content_sha256"],
            "accepted_sha256": entry["candidate_sha256"],
            "status": "ACCEPTED",
        })
        self.store.save_authority_ledger(ledger)
        self._remote_commit(guide="R3\n", message="guide R3")
        result = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(result.status, "AUTHORITY_CHANGE_DETECTED")
        self.assertNotEqual(result.new_change["change_id"], first.new_change["change_id"])
        self.assertEqual(len(list(self.store.authority_changes_path.glob("CR-*.json"))), 2)

    def test_unrelated_commit_during_pending_candidate_does_not_report_new_change(self):
        check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(guide="R2\n", message="guide R2")
        first = check_remote_authority(self.config, self.store, self.state)
        self._remote_commit(unrelated="three\n", message="validator infrastructure")
        result = check_remote_authority(self.config, self.store, self.state)
        dry = check_remote_authority(self.config, self.store, self.state, dry_run=True)
        self.assertEqual(first.status, "AUTHORITY_CHANGE_DETECTED")
        self.assertEqual(result.status, "CHANGE_PENDING")
        self.assertFalse(result.changed)
        self.assertEqual(dry.status, "CHANGE_PENDING")
        self.assertFalse(dry.changed)
        self.assertEqual(len(list(self.store.authority_changes_path.glob("CR-*.json"))), 1)

    def test_local_draft_is_ignored_when_remote_is_unchanged(self):
        check_remote_authority(self.config, self.store, self.state)
        self.guide.write_text("local R3 draft\n", encoding="utf-8")
        result = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(result.status, "NO_CHANGE")
        self.assertEqual(len(list(self.store.authority_changes_path.glob("CR-*.json"))), 0)

    def test_missing_exact_path_does_not_guess_rename(self):
        missing_workflow = self.workflow.read_text().replace("path: guide.md", "path: missing-guide.md")
        missing_workflow = missing_workflow.replace("sha256: " + self.old_sha, "sha256: " + self.old_sha)
        self.workflow.write_text(missing_workflow, encoding="utf-8")
        config = load_workflow(self.workflow, self.project)
        result = check_remote_authority(config, StateStore(self.root / "missing-state"), self.state)
        self.assertEqual(result.status, REMOTE_AUTHORITY_SOURCE_MISSING)
        self.assertTrue(result.errors[0].startswith(REMOTE_AUTHORITY_SOURCE_MISSING))
        remote_state = json.loads((self.root / "missing-state" / "authority" / "remote-state.json").read_text())
        self.assertEqual(remote_state["sources"]["human-guide"]["last_seen_remote_commit"], result.snapshot.commit_sha)

    def test_fetch_failure_preserves_ledger(self):
        check_remote_authority(self.config, self.store, self.state)
        before = self.store.authority_ledger_path.read_text()
        broken = self.workflow.read_text().replace("remote: origin", "remote: missing-remote")
        self.workflow.write_text(broken, encoding="utf-8")
        config = load_workflow(self.workflow, self.project)
        result = check_remote_authority(config, self.store, self.state)
        self.assertEqual(result.status, REMOTE_CHECK_FAILED)
        self.assertEqual(self.store.authority_ledger_path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
