from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from contract_workflow.authority_set import aggregate_authority_set_hash, member_change_sets
from contract_workflow.config import load_workflow
from contract_workflow.models import WorkflowState
from contract_workflow.remote import NEWER_REMOTE_REVISION_AVAILABLE, check_remote_authority
from contract_workflow.state_store import StateStore


class AuthoritySetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.remote = self.root / "remote.git"
        self.project.mkdir()
        (self.project / "human-authority" / "engineering-directives").mkdir(parents=True)
        (self.project / "human-authority" / "references").mkdir(parents=True)
        (self.project / "guide.md").write_text("R1\n", encoding="utf-8")
        (self.project / "human-authority" / "engineering-directives" / "HED-001.md").write_text("schema-first\n", encoding="utf-8")
        (self.project / "human-authority" / "references" / "REF-MCP-001.md").write_text("pattern\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "init", "-q", "-b", "main"], check=True)
        self._commit("baseline")
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.project), "remote", "add", "origin", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(self.project), "push", "-q", "-u", "origin", "main"], check=True)
        self.state_root = self.root / "state"
        os.environ["CWO_STATE_DIR"] = str(self.state_root)
        self.workflow = self.project / ".contract-workflow" / "workflow.yaml"
        self.workflow.parent.mkdir()
        guide_hash = hashlib.sha256(b"R1\n").hexdigest()
        self.workflow.write_text(f'''version: "1"
project:
  name: authority-set-fixture
  path: {self.project}
mode: autonomous
authority:
  remote: origin
  branch: main
  members:
    - id: architecture-guide
      role: ARCHITECTURE_GUIDE
      path: guide.md
    - id: HED-001
      role: ENGINEERING_DIRECTIVE
      path: human-authority/engineering-directives/HED-001.md
    - id: REF-MCP-001
      role: REFERENCE_POLICY
      path: human-authority/references/REF-MCP-001.md
authoritative_sources:
  - source_id: human-guide
    role: HUMAN_GUIDE
    path: guide.md
    sha256: {guide_hash}
skills: {{}}
runner:
  type: mock
groups:
  - id: g
    tasks:
      - id: task
''', encoding="utf-8")
        self.config = load_workflow(self.workflow, self.project)
        self.store = StateStore(self.state_root)
        self.state = WorkflowState(project=self.config.project_name, project_path=str(self.project), workflow_file=str(self.workflow), workflow_digest=self.config.digest)

    def tearDown(self) -> None:
        os.environ.pop("CWO_STATE_DIR", None)
        self.temp.cleanup()

    def _commit(self, message: str) -> None:
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=CWO", "-c", "user.email=cwo@example.invalid", "commit", "-qm", message], check=True)

    def _push_change(self, path: str, content: str, message: str) -> None:
        target = self.project / path
        target.write_text(content, encoding="utf-8")
        self._commit(message)
        subprocess.run(["git", "-C", str(self.project), "push", "-q", "origin", "main"], check=True)

    def test_aggregate_hash_is_order_independent_and_member_diff_is_explicit(self) -> None:
        first = [{"member_id": "b", "role": "REFERENCE_POLICY", "path": "b", "content_sha256": "2"}, {"member_id": "a", "role": "ARCHITECTURE_GUIDE", "path": "a", "content_sha256": "1"}]
        second = list(reversed(first))
        self.assertEqual(aggregate_authority_set_hash(first), aggregate_authority_set_hash(second))
        changed = [{**first[1], "content_sha256": "3"}, first[0], {"member_id": "c", "role": "PROJECT_DECISION", "path": "c", "content_sha256": "4"}]
        self.assertEqual(member_change_sets(first, changed), {"unchanged": ["b"], "modified": ["a"], "added": ["c"], "removed": []})

    def test_first_set_observation_and_unrelated_commit_are_no_change(self) -> None:
        first = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(first.status, "NO_CHANGE")
        self._push_change("unrelated.txt", "one\n", "unrelated")
        second = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(second.status, "NO_CHANGE")
        self.assertIsNotNone(second.authority_set)

    def test_member_change_creates_set_candidate_and_rollover_reuses_cr(self) -> None:
        check_remote_authority(self.config, self.store, self.state)
        self._push_change("human-authority/engineering-directives/HED-001.md", "schema-first v2\n", "HED change")
        first = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(first.status, "AUTHORITY_CHANGE_DETECTED")
        change_id = first.new_change["change_id"]
        change = json.loads((self.store.authority_changes_path / f"{change_id}.json").read_text())
        self.assertEqual(change["authority_set_member_changes"]["modified"], ["HED-001"])
        self.assertEqual(change["authority_set_member_changes"]["unchanged"], ["REF-MCP-001", "architecture-guide"])
        self._push_change("human-authority/references/REF-MCP-001.md", "pattern v2\n", "reference change")
        rolled = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(rolled.status, NEWER_REMOTE_REVISION_AVAILABLE)
        self.assertEqual(rolled.rollover["change_id"], change_id)
        self.assertEqual(rolled.rollover["new_authority_set_hash"], rolled.authority_set.aggregate_hash)
        final = json.loads((self.store.authority_changes_path / f"{change_id}.json").read_text())
        self.assertEqual(len(final["candidate_revisions"]), 2)
        self.assertEqual(final["candidate_revisions"][0]["status"], "SUPERSEDED")
        self.assertEqual(final["candidate_revisions"][1]["status"], "ACTIVE")

    def test_local_guide_draft_does_not_change_remote_set(self) -> None:
        check_remote_authority(self.config, self.store, self.state)
        (self.project / "guide.md").write_text("local draft\n", encoding="utf-8")
        result = check_remote_authority(self.config, self.store, self.state)
        self.assertEqual(result.status, "NO_CHANGE")


if __name__ == "__main__":
    unittest.main()
