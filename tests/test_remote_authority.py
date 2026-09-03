from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.config import load_workflow
from contract_workflow.models import WorkflowState
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
