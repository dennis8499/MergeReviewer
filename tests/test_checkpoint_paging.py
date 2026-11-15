"""Checkpoint-only commits, interrupted transactions and full paging recovery."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import SCRIPTS
from tests.fixtures import ReviewFixture
import review_author_operation as author
import review_checkpoint as checkpoint
import review_coordinator as coordination
import review_flow as flow
import review_paging as paging
import review_runtime


class PagingCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paging-cas-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identifier = "a" * 32
        self.context = self.root / "sessions" / self.identifier
        self.context.mkdir(parents=True)
        self.manifest = {"repositories": [{"id": "repo-000", "sources": {"head": {"ref": "b" * 40}}}],
                         "operations": {}}
        self.draft = {"schema": "merge-reviewer-authoring/v1", "summary": "Initial judgment"}
        coordination.atomic(self.context / "draft.json", self.draft)
        coordination.atomic(self.context / "manifest.json", self.manifest)
        artifact = review_runtime.store_view(self.context, {"lines": [{"line": 1, "text": "return amount * quantity"}]})
        self.state = paging.new_state(self.root, self.identifier, [{
            "artifact": artifact, "artifact_scope": "context", "pointer": "/lines", "offset": 0, "page_size": 70}])
        self.diagnostics = mock.patch.object(flow, "diagnostics", return_value={"errors": [], "gaps": []})
        self.diagnostics.start()
        self.addCleanup(self.diagnostics.stop)

    def operation(self, identifier, revision, state=None):
        return {"id": identifier, "op": "author", "updates": {},
                "expected_draft_sha256": coordination.file_digest(self.context / "draft.json"),
                "expected_checkpoint_revision": revision,
                "checkpoint": {"next_action": "Continue necessary evidence", "paging": state or self.state}}

    def test_checkpoint_only_commit_advances_revision_without_changing_draft(self):
        before = (self.context / "draft.json").read_bytes()
        first = author.apply(self.context, self.manifest, self.operation("save_one", 0))
        second = author.apply(self.context, self.manifest, self.operation("save_two", 1))
        self.assertEqual(before, (self.context / "draft.json").read_bytes())
        self.assertEqual((1, 2), (first["checkpoint"]["checkpoint_revision"], second["checkpoint"]["checkpoint_revision"]))
        self.assertEqual(second["checkpoint"], checkpoint.load(self.context))
        with self.assertRaisesRegex(ValueError, "checkpoint revision conflict"):
            author.apply(self.context, self.manifest, self.operation("stale_new_id", 1))
        self.assertEqual(2, checkpoint.revision(checkpoint.load(self.context)))

    def test_old_successful_journal_cannot_roll_back_newer_checkpoint(self):
        old = self.operation("old_save", 0)
        author.apply(self.context, self.manifest, old)
        author.apply(self.context, self.manifest, self.operation("new_save", 1))
        before = (self.context / "checkpoint.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "checkpoint revision conflict"):
            author.apply(self.context, self.manifest, old)
        self.assertEqual(before, (self.context / "checkpoint.json").read_bytes())

    def test_checkpoint_commit_recovers_after_draft_or_checkpoint_interruption(self):
        original = coordination.atomic
        for interrupted_name in ("draft.json", "checkpoint.json"):
            with self.subTest(stage=interrupted_name):
                revision = checkpoint.revision(checkpoint.load(self.context))
                operation = self.operation("interrupted_" + interrupted_name.split(".")[0], revision)
                operation["updates"] = {"summary": "Recovered judgment after " + interrupted_name}
                def interrupted(path, value):
                    if path.name == interrupted_name:
                        raise OSError("simulated interruption")
                    return original(path, value)
                with mock.patch.object(coordination, "atomic", interrupted), self.assertRaisesRegex(OSError, "simulated"):
                    author.apply(self.context, self.manifest, operation)
                recovered = author.apply(self.context, self.manifest, operation)
                with mock.patch.object(coordination, "atomic", side_effect=AssertionError("committed retry wrote state")):
                    retried = author.apply(self.context, self.manifest, operation)
                self.assertEqual(revision + 1, recovered["checkpoint"]["checkpoint_revision"])
                self.assertEqual(recovered, retried)
                self.assertEqual(recovered["checkpoint"], checkpoint.load(self.context))
                self.assertEqual(operation["updates"]["summary"], coordination.read_json(self.context / "draft.json")["summary"])
                self.assertEqual(coordination.file_digest(self.context / "draft.json"), recovered["checkpoint"]["draft_sha256"])

    def test_large_paging_state_recovers_from_summary_reference_without_writes(self):
        state = copy.deepcopy(self.state)
        state["acknowledged"] = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(1400)]
        saved = author.apply(self.context, self.manifest, self.operation("large_save", 0, state))
        jobs = self.root / "jobs"
        jobs.mkdir()
        coordination.atomic(jobs / (self.identifier + ".json"), {
            "schema": flow.SCHEMA, "request_id": self.identifier, "runtime_root": str(self.root),
            "context_dir": str(self.context), "state": "reviewing", "phase": "review",
            "execution_checkpoint": {"next_action": "stale cached job"}})
        with mock.patch.object(coordination, "atomic", side_effect=AssertionError("read wrote state")):
            summary = flow.status(self.root, self.identifier, summary=True)
            artifact = summary["paging_ref"]["artifact"]
            import git_review_context as api
            with mock.patch.object(api, "run_git", side_effect=AssertionError("read ran Git")):
                first, _ = flow.read_artifact(self.root, self.identifier, artifact, scope="context")
                self.assertFalse(first["view_complete"])
                restored = {key: value for field in first["value"]["fields"] for key, value in field.items()}
                for continuation in first.get("drilldowns", []):
                    field = continuation["pointer"][1:]
                    parts = []
                    while continuation is not None:
                        page, _ = flow.read_artifact(self.root, self.identifier, artifact, scope="context",
                                      pointer=continuation["pointer"], offset=continuation.get("offset", 0),
                                      page_size=continuation.get("page_size", 100))
                        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode("utf-8")), flow.LIMIT)
                        parts.extend(page["items"])
                        continuation = page.get("continue")
                    restored[field] = parts
        self.assertLess(len(json.dumps(summary, ensure_ascii=False).encode("utf-8")), flow.LIMIT)
        self.assertEqual(1, summary["checkpoint_revision"])
        self.assertEqual(1400, summary["paging_counts"]["acknowledged"])
        self.assertEqual(state, restored)
        self.assertEqual(saved["checkpoint"]["paging_ref"], summary["paging_ref"])

    def test_legacy_author_preserves_paging_and_new_paging_requires_revision(self):
        saved = author.apply(self.context, self.manifest, self.operation("first", 0))
        legacy = {"id": "legacy", "op": "author", "updates": {},
                  "expected_draft_sha256": coordination.file_digest(self.context / "draft.json"),
                  "checkpoint": {"next_action": "Inspect next chain"}}
        updated = author.apply(self.context, self.manifest, legacy)
        self.assertEqual(saved["checkpoint"]["paging_ref"], updated["checkpoint"]["paging_ref"])
        missing = self.operation("missing_revision", 2)
        del missing["expected_checkpoint_revision"]
        with self.assertRaisesRegex(ValueError, "expected_checkpoint_revision"):
            author.apply(self.context, self.manifest, missing)

    def test_cross_snapshot_state_is_rejected_before_draft_commit(self):
        bad = copy.deepcopy(self.state)
        bad["source_identity"] = "different-snapshot"
        before = (self.context / "draft.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "source_identity"):
            author.apply(self.context, self.manifest, self.operation("wrong_snapshot", 0, bad))
        self.assertEqual(before, (self.context / "draft.json").read_bytes())
        self.assertIsNone(checkpoint.load(self.context))


class PagingFlowIntegrationTests(ReviewFixture):
    def test_cached_author_replay_preserves_latest_checkpoint_and_paging_artifact(self):
        self.commit()
        args = flow.parser().parse_args(["start", "--workspace", str(self.root), "--project", str(self.repo),
                "--base", "main", "--head", "feature", "--runtime-root", str(self.root / "runtime"),
                "--draft-format", "compact"])
        from contextlib import redirect_stderr
        import io
        with redirect_stderr(io.StringIO()):
            started = flow.start(args)
        root, identifier, context = Path(started["runtime_root"]), started["request_id"], Path(started["context_dir"])
        self.addCleanup(__import__("review_session").cleanup_session, context)
        state = paging.new_state(root, identifier, [])
        old = {"id": "save_old", "op": "author", "updates": {"summary": "Inspect fixed required changes."},
               "expected_draft_sha256": coordination.file_digest(context / "draft.json"),
               "expected_checkpoint_revision": 0, "checkpoint": {"paging": state, "next_action": "First chain"}}
        with redirect_stderr(io.StringIO()):
            first = flow.step(root, identifier, [old])
            self.assertTrue(first["complete"], first)
            newer = {**old, "id": "save_new", "expected_checkpoint_revision": 1,
                     "expected_draft_sha256": coordination.file_digest(context / "draft.json"),
                     "checkpoint": {"paging": state, "next_action": "Second chain"}}
            second = flow.step(root, identifier, [newer])
            self.assertTrue(second["complete"], second)
            repeated = flow.step(root, identifier, [old])
        self.assertTrue(repeated["complete"], repeated)
        self.assertTrue(repeated["operations"][0]["reused"])
        summary = flow.status(root, identifier, summary=True)
        self.assertEqual(2, summary["checkpoint_revision"])
        self.assertEqual("Second chain", summary["execution_checkpoint"]["next_action"])
        self.assertEqual(state, review_runtime.load_view(context, summary["paging_ref"]["artifact"]))
        self.assertEqual(2, coordination.read_json(root / "jobs" / (identifier + ".json"))["execution_checkpoint"]["checkpoint_revision"])


if __name__ == "__main__":
    unittest.main()
